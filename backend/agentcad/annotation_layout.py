from __future__ import annotations

import re
from dataclasses import dataclass
from math import hypot

from .agent_semantic_models import AnnotationLayoutMetrics, AnnotationQuality
from .models import (
    AddElementOperation,
    ConnectorElement,
    DeleteElementOperation,
    Document,
    Element,
    LineElement,
    Operation,
    Point,
    Style,
    TextElement,
    TransactionRequest,
    UpdateElementOperation,
)
from .service import DocumentService
from .symbols import SymbolRegistry

TEXT_MARGIN = 6.0
DUPLICATE_RADIUS = 220.0


@dataclass(frozen=True)
class Rect:
    x1: float
    y1: float
    x2: float
    y2: float
    element_id: str = ""

    @property
    def center(self) -> Point:
        return Point(x=(self.x1 + self.x2) / 2, y=(self.y1 + self.y2) / 2)

    def expanded(self, margin: float) -> Rect:
        return Rect(
            x1=self.x1 - margin,
            y1=self.y1 - margin,
            x2=self.x2 + margin,
            y2=self.y2 + margin,
            element_id=self.element_id,
        )


def normalize_annotation_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def text_bounds(text: TextElement) -> Rect:
    width = max(text.font_size, len(text.text) * text.font_size * 0.6)
    offset = width / 2 if text.anchor == "middle" else width if text.anchor == "end" else 0
    return Rect(
        x1=text.position.x - offset,
        y1=text.position.y - text.font_size,
        x2=text.position.x - offset + width,
        y2=text.position.y + text.font_size * 0.3,
        element_id=text.id,
    )


def symbol_bounds(element: Element) -> Rect | None:
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


def _symbol_label_text(element: Element, registry: SymbolRegistry) -> TextElement | None:
    if element.type != "symbol" or not element.label.strip():
        return None
    try:
        definition = registry.get(element.symbol_key)
    except KeyError:
        # The drawing references a symbol the loaded catalog does not define. Its label
        # bounds cannot be derived, so it takes part in no annotation decision instead of
        # raising: a missing definition is a data problem the reports already flag.
        return None
    scale_x = element.width / definition.width
    scale_y = element.height / definition.height
    return TextElement(
        id=f"{element.id}::virtual-label",
        position=Point(
            x=element.position.x + element.width / 2,
            y=element.position.y + element.height + 16 * scale_y,
        ),
        text=element.label,
        font_size=max(8.0, 12 * min(scale_x, scale_y)),
        anchor="middle",
        style=Style(
            stroke=element.style.stroke,
            opacity=element.style.opacity,
        ),
        metadata={"parent_element_id": element.id, "virtual_symbol_label": True},
    )


def _rects_overlap(left: Rect, right: Rect) -> bool:
    return not (
        left.x2 <= right.x1
        or right.x2 <= left.x1
        or left.y2 <= right.y1
        or right.y2 <= left.y1
    )


def _segment_intersects_rect(start: Point, end: Point, rect: Rect) -> bool:
    if start.x == end.x:
        return rect.x1 <= start.x <= rect.x2 and max(min(start.y, end.y), rect.y1) <= min(
            max(start.y, end.y), rect.y2
        )
    if start.y == end.y:
        return rect.y1 <= start.y <= rect.y2 and max(min(start.x, end.x), rect.x1) <= min(
            max(start.x, end.x), rect.x2
        )
    return _rects_overlap(
        Rect(min(start.x, end.x), min(start.y, end.y), max(start.x, end.x), max(start.y, end.y)),
        rect,
    )


def _connector_intersects_rect(connector: ConnectorElement, rect: Rect) -> bool:
    expanded = rect.expanded(2)
    return any(
        _segment_intersects_rect(first, second, expanded)
        for first, second in zip(connector.points, connector.points[1:], strict=False)
    )


def _annotations(document: Document, registry: SymbolRegistry) -> list[TextElement]:
    result = [element for element in document.elements if element.type == "text"]
    result.extend(
        label
        for element in document.elements
        if (label := _symbol_label_text(element, registry)) is not None
    )
    return result


def _annotation_parent_id(text: TextElement) -> str | None:
    parent_id = text.metadata.get("parent_element_id")
    return parent_id if isinstance(parent_id, str) and parent_id else None


def _annotations_are_duplicates(left: TextElement, right: TextElement) -> bool:
    if left.metadata.get("keep_duplicate") is True or right.metadata.get("keep_duplicate") is True:
        return False
    if normalize_annotation_text(left.text) != normalize_annotation_text(right.text):
        return False
    left_parent = _annotation_parent_id(left)
    right_parent = _annotation_parent_id(right)
    return not (left_parent and right_parent and left_parent != right_parent)


def measure_annotation_quality(document: Document, registry: SymbolRegistry) -> AnnotationQuality:
    annotations = _annotations(document, registry)
    text_rects = [(text, text_bounds(text)) for text in annotations if text.text.strip()]
    symbol_rects = [
        rect for element in document.elements if (rect := symbol_bounds(element)) is not None
    ]
    connectors = [element for element in document.elements if element.type == "connector"]

    duplicates = 0
    for index, (left, left_rect) in enumerate(text_rects):
        for right, right_rect in text_rects[index + 1 :]:
            if not _annotations_are_duplicates(left, right):
                continue
            if hypot(
                left_rect.center.x - right_rect.center.x,
                left_rect.center.y - right_rect.center.y,
            ) <= DUPLICATE_RADIUS:
                duplicates += 1

    text_text = sum(
        1
        for index, (_, left) in enumerate(text_rects)
        for _, right in text_rects[index + 1 :]
        if _rects_overlap(left.expanded(2), right.expanded(2))
    )
    text_symbol = sum(
        1
        for _, text_rect in text_rects
        for symbol_rect in symbol_rects
        if _rects_overlap(text_rect.expanded(2), symbol_rect.expanded(2))
    )
    text_connector = sum(
        1
        for _, text_rect in text_rects
        for connector in connectors
        if _connector_intersects_rect(connector, text_rect)
    )
    return AnnotationQuality(
        duplicate_label_count=duplicates,
        text_text_overlaps=text_text,
        text_symbol_overlaps=text_symbol,
        text_connector_intersections=text_connector,
    )


def _unique_id(document: Document, base: str) -> str:
    existing = {element.id for element in document.elements}
    if base not in existing:
        return base
    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"


def _apply(service: DocumentService, document: Document, operation: Operation) -> None:
    service._apply_operation(document, operation)


def _text_candidate(
    text: TextElement,
    x: float,
    y: float,
    anchor: str,
) -> TextElement:
    return text.model_copy(
        update={
            "position": Point(x=x, y=y),
            "anchor": anchor,
        },
        deep=True,
    )


def _parent_candidates(text: TextElement, parent: Element) -> list[tuple[TextElement, bool]]:
    rect = symbol_bounds(parent)
    if rect is None:
        return []
    font = text.font_size
    center_y = (rect.y1 + rect.y2) / 2 + font * 0.3
    close = 14.0
    far = 70.0
    above = _text_candidate(text, (rect.x1 + rect.x2) / 2, rect.y1 - close, "middle")
    below = _text_candidate(
        text,
        (rect.x1 + rect.x2) / 2,
        rect.y2 + font + close,
        "middle",
    )
    stacked_below = _text_candidate(
        text,
        (rect.x1 + rect.x2) / 2,
        rect.y2 + font + 38,
        "middle",
    )
    right = _text_candidate(text, rect.x2 + close, center_y, "start")
    left = _text_candidate(text, rect.x1 - close, center_y, "end")
    far_above = _text_candidate(text, (rect.x1 + rect.x2) / 2, rect.y1 - far, "middle")
    far_below = _text_candidate(
        text,
        (rect.x1 + rect.x2) / 2,
        rect.y2 + font + far,
        "middle",
    )
    far_right = _text_candidate(text, rect.x2 + far, center_y, "start")
    far_left = _text_candidate(text, rect.x1 - far, center_y, "end")
    very_far = 120.0
    very_far_candidates = [
        _text_candidate(text, (rect.x1 + rect.x2) / 2, rect.y1 - very_far, "middle"),
        _text_candidate(
            text,
            (rect.x1 + rect.x2) / 2,
            rect.y2 + font + very_far,
            "middle",
        ),
        _text_candidate(text, rect.x2 + very_far, center_y, "start"),
        _text_candidate(text, rect.x1 - very_far, center_y, "end"),
    ]
    search_candidates: list[TextElement] = []
    center_x = (rect.x1 + rect.x2) / 2
    for distance in (90.0, 120.0, 160.0, 200.0):
        search_candidates.extend(
            [
                _text_candidate(text, center_x - distance, rect.y1 - distance, "middle"),
                _text_candidate(text, center_x + distance, rect.y1 - distance, "middle"),
                _text_candidate(
                    text,
                    center_x - distance,
                    rect.y2 + font + distance,
                    "middle",
                ),
                _text_candidate(
                    text,
                    center_x + distance,
                    rect.y2 + font + distance,
                    "middle",
                ),
            ]
        )

    instrument = parent.metadata.get("assembly") == "instrument_tap" and parent.metadata.get(
        "role"
    ) == "instrument"
    close_candidates = (
        [above, right, left, below]
        if instrument
        else [below, stacked_below, above, right, left]
    )
    far_candidates = (
        [far_above, far_right, far_left, far_below]
        if instrument
        else [far_below, far_above, far_right, far_left]
    )
    return (
        [(candidate, False) for candidate in close_candidates]
        + [(candidate, True) for candidate in far_candidates]
        + [(candidate, True) for candidate in very_far_candidates]
        + [(candidate, True) for candidate in search_candidates]
    )


def _free_candidates(text: TextElement) -> list[tuple[TextElement, bool]]:
    x = text.position.x
    y = text.position.y
    offsets = [
        (0, 0),
        (0, -28),
        (0, 28),
        (40, 0),
        (-40, 0),
        (60, -36),
        (-60, -36),
    ]
    for distance in (60, 90, 120, 160):
        offsets.extend(
            [
                (0, -distance),
                (0, distance),
                (distance, 0),
                (-distance, 0),
                (distance, -distance),
                (-distance, -distance),
                (distance, distance),
                (-distance, distance),
            ]
        )
    return [(_text_candidate(text, x + dx, y + dy, text.anchor), False) for dx, dy in offsets]


def _candidate_score(
    text: TextElement,
    document: Document,
    symbol_rects: list[Rect],
    placed_text_rects: list[Rect],
    connectors: list[ConnectorElement],
) -> int:
    rect = text_bounds(text)
    score = 0
    if rect.x1 < 0 or rect.y1 < 0 or rect.x2 > document.canvas.width or rect.y2 > document.canvas.height:
        score += 100
    score += 20 * sum(
        1 for symbol_rect in symbol_rects if _rects_overlap(rect.expanded(TEXT_MARGIN), symbol_rect)
    )
    score += 30 * sum(
        1 for placed in placed_text_rects if _rects_overlap(rect.expanded(2), placed.expanded(2))
    )
    score += 10 * sum(
        1 for connector in connectors if _connector_intersects_rect(connector, rect)
    )
    return score


def _leader_points(parent: Element, text: TextElement) -> tuple[Point, Point] | None:
    parent_rect = symbol_bounds(parent)
    if parent_rect is None:
        return None
    target = text_bounds(text).center
    center = parent_rect.center
    dx = target.x - center.x
    dy = target.y - center.y
    if abs(dx) >= abs(dy):
        start = Point(
            x=parent_rect.x2 if dx >= 0 else parent_rect.x1,
            y=max(parent_rect.y1, min(target.y, parent_rect.y2)),
        )
    else:
        start = Point(
            x=max(parent_rect.x1, min(target.x, parent_rect.x2)),
            y=parent_rect.y2 if dy >= 0 else parent_rect.y1,
        )
    return start, target


def polish_full_diagram_transaction(
    service: DocumentService,
    document_id: str,
    transaction: TransactionRequest,
) -> tuple[TransactionRequest, AnnotationLayoutMetrics]:
    current = service.get_document(document_id)
    working = Document.model_validate(current.model_dump(mode="python"))
    for operation in transaction.operations:
        _apply(service, working, operation)
    before = measure_annotation_quality(working, service.symbols)
    operations: list[Operation] = list(transaction.operations)
    generated_text_ids: list[str] = []
    moved_text_ids: list[str] = []
    deleted_text_ids: list[str] = []
    leader_line_ids: list[str] = []

    # Convert fixed symbol labels into normal, editable text annotations.
    for symbol in sorted(
        (element for element in list(working.elements) if element.type == "symbol" and element.label.strip()),
        key=lambda element: element.id,
    ):
        label_id = _unique_id(working, f"{symbol.id}__label")
        text = TextElement(
            id=label_id,
            position=Point(
                x=symbol.position.x + symbol.width / 2,
                y=symbol.position.y + symbol.height + 18,
            ),
            text=symbol.label.strip(),
            font_size=12,
            anchor="middle",
            layer_id=symbol.layer_id,
            system_id=symbol.system_id,
            style=Style(stroke=symbol.style.stroke, opacity=symbol.style.opacity),
            metadata={
                "parent_element_id": symbol.id,
                "annotation_role": "symbol_label",
                "generated_by": "annotation_layout",
            },
        )
        update = UpdateElementOperation(element_id=symbol.id, patch={"label": ""})
        add = AddElementOperation(element=text)
        _apply(service, working, update)
        _apply(service, working, add)
        operations.extend([update, add])
        generated_text_ids.append(text.id)

    # Keep one nearby instance of the same annotation for the same subject.
    kept: list[TextElement] = []
    texts = sorted(
        (element for element in list(working.elements) if element.type == "text" and element.text.strip()),
        key=lambda element: (
            0 if element.metadata.get("parent_element_id") else 1,
            element.id,
        ),
    )
    for text in texts:
        rect = text_bounds(text)
        duplicate = next(
            (
                other
                for other in kept
                if _annotations_are_duplicates(other, text)
                and hypot(
                    text_bounds(other).center.x - rect.center.x,
                    text_bounds(other).center.y - rect.center.y,
                )
                <= DUPLICATE_RADIUS
            ),
            None,
        )
        if duplicate is None:
            kept.append(text)
            continue
        delete = DeleteElementOperation(element_id=text.id)
        _apply(service, working, delete)
        operations.append(delete)
        deleted_text_ids.append(text.id)

    element_map = {element.id: element for element in working.elements}
    symbol_rects = [
        rect.expanded(4)
        for element in working.elements
        if (rect := symbol_bounds(element)) is not None
    ]
    connectors = [element for element in working.elements if element.type == "connector"]
    placed: list[Rect] = []

    for text in sorted(
        (element for element in list(working.elements) if element.type == "text" and element.text.strip()),
        key=lambda element: (
            0 if element.metadata.get("parent_element_id") else 1,
            element.id,
        ),
    ):
        parent_id = text.metadata.get("parent_element_id")
        parent = element_map.get(parent_id) if isinstance(parent_id, str) else None
        candidates = _parent_candidates(text, parent) if parent is not None else _free_candidates(text)
        if not candidates:
            candidates = _free_candidates(text)
        scored = [
            (
                _candidate_score(candidate, working, symbol_rects, placed, connectors),
                index,
                candidate,
                remote,
            )
            for index, (candidate, remote) in enumerate(candidates)
        ]
        _, _, selected, remote = min(scored, key=lambda item: (item[0], item[1]))
        if selected.position != text.position or selected.anchor != text.anchor:
            update = UpdateElementOperation(
                element_id=text.id,
                patch={
                    "position": selected.position.model_dump(mode="json"),
                    "anchor": selected.anchor,
                },
            )
            _apply(service, working, update)
            operations.append(update)
            moved_text_ids.append(text.id)
            text = next(
                element
                for element in working.elements
                if element.id == text.id and element.type == "text"
            )
        placed.append(text_bounds(text))

        if remote and parent is not None:
            leader_points = _leader_points(parent, text)
            if leader_points is not None:
                leader_id = _unique_id(working, f"{text.id}__leader")
                leader = LineElement(
                    id=leader_id,
                    start=leader_points[0],
                    end=leader_points[1],
                    layer_id=text.layer_id,
                    system_id=text.system_id,
                    style=Style(
                        stroke=text.style.stroke,
                        stroke_width=0.8,
                        opacity=0.8,
                        dash=[4, 3],
                    ),
                    metadata={
                        "annotation_role": "leader_line",
                        "parent_element_id": parent.id,
                        "text_element_id": text.id,
                        "generated_by": "annotation_layout",
                    },
                )
                add = AddElementOperation(element=leader)
                _apply(service, working, add)
                operations.append(add)
                leader_line_ids.append(leader.id)

    working = Document.model_validate(working.model_dump(mode="python"))
    after = measure_annotation_quality(working, service.symbols)
    polished = TransactionRequest(
        operations=operations,
        expected_revision=transaction.expected_revision,
        label=transaction.label,
        source=transaction.source,
    )
    metrics = AnnotationLayoutMetrics(
        before=before,
        after=after,
        generated_text_ids=generated_text_ids,
        moved_text_ids=sorted(set(moved_text_ids)),
        deleted_text_ids=sorted(set(deleted_text_ids)),
        leader_line_ids=leader_line_ids,
    )
    return polished, metrics
