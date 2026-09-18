from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .models import Document, StrictModel, TransactionRequest


class RegionBox(StrictModel):
    """An axis-aligned rectangle in drawing space, used by layout/drafting regions."""

    x1: float
    y1: float
    x2: float
    y2: float
    label: str = ""

    @property
    def width(self) -> float:
        return abs(self.x2 - self.x1)

    @property
    def height(self) -> float:
        return abs(self.y2 - self.y1)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            min(self.x1, self.x2),
            min(self.y1, self.y2),
            max(self.x1, self.x2),
            max(self.y1, self.y2),
        )

    def normalized(self) -> RegionBox:
        return RegionBox(
            x1=min(self.x1, self.x2),
            y1=min(self.y1, self.y2),
            x2=max(self.x1, self.x2),
            y2=max(self.y1, self.y2),
            label=self.label,
        )

    def contains_point(self, x: float, y: float) -> bool:
        left, top, right, bottom = self.bounds
        return left <= x <= right and top <= y <= bottom

    def contains_box(self, x1: float, y1: float, x2: float, y2: float) -> bool:
        left, top, right, bottom = self.bounds
        return left <= min(x1, x2) and top <= min(y1, y2) and right >= max(x1, x2) and bottom >= max(y1, y2)

    def overlaps_box(self, x1: float, y1: float, x2: float, y2: float) -> bool:
        left, top, right, bottom = self.bounds
        return not (
            right <= min(x1, x2)
            or left >= max(x1, x2)
            or bottom <= min(y1, y2)
            or top >= max(y1, y2)
        )


class LayoutRegion(RegionBox):
    """A declared drawing region that drafting must respect.

    ``kind`` is what the region *means*, not where it is:

    * ``lock`` — human-confirmed work. Any element whose geometry sits inside it is
      locked, so automatic layout must arrange other elements around it.
    * ``legend`` / ``title_block`` / ``notes`` / ``keep_clear`` — reserved drawing
      space (Charter §6.2 border/title block, §15 legend overlap). Pipes, symbols
      and labels must not intrude into it.
    """

    kind: Literal["lock", "legend", "title_block", "notes", "keep_clear"] = "lock"

    def normalized(self) -> LayoutRegion:
        box = RegionBox.normalized(self)
        return LayoutRegion(
            x1=box.x1,
            y1=box.y1,
            x2=box.x2,
            y2=box.y2,
            label=self.label,
            kind=self.kind,
        )


#: Document metadata key holding the declared regions above. It is plain document
#: data (set by any ordinary audited transaction), never written by an engine.
LAYOUT_REGIONS_KEY = "layout_regions"


def declared_layout_regions(document: Document) -> list[LayoutRegion]:
    """Read declared layout regions from document metadata, deterministically sorted.

    Unparseable entries are ignored rather than guessed at: a malformed region must
    never silently become a lock that freezes editing or a reserved block that
    swallows the drawing.
    """

    raw: Any = document.metadata.get(LAYOUT_REGIONS_KEY)
    if isinstance(raw, LayoutRegion):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    regions: list[LayoutRegion] = []
    for entry in raw:
        try:
            regions.append(entry if isinstance(entry, LayoutRegion) else LayoutRegion.model_validate(entry))
        except Exception:
            continue
    return sorted(
        regions,
        key=lambda region: (region.kind, region.x1, region.y1, region.x2, region.y2, region.label),
    )


def declared_lock_regions(document: Document) -> list[LayoutRegion]:
    return [region for region in declared_layout_regions(document) if region.kind == "lock"]


def declared_reserved_regions(document: Document) -> list[LayoutRegion]:
    return [region for region in declared_layout_regions(document) if region.kind != "lock"]


#: Element metadata flag that locks one element against automatic movement and
#: re-routing. It is document data, so it persists across sessions and is visible to
#: every layout surface instead of living in one caller's request.
ELEMENT_LOCK_KEY = "drafting_lock"


def element_is_locked(element: Any) -> bool:
    value = getattr(element, "metadata", {}).get(ELEMENT_LOCK_KEY)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "locked"}
    return bool(value)


class AutoLayoutRequest(StrictModel):
    expected_revision: int | None = Field(default=None, ge=0)
    element_ids: list[str] = Field(default_factory=list, max_length=5000)
    locked_element_ids: list[str] = Field(default_factory=list, max_length=5000)
    direction: Literal["horizontal", "vertical"] = "horizontal"
    rank_gap: float = Field(default=180, ge=60, le=1000)
    node_gap: float = Field(default=90, ge=20, le=500)
    component_gap: float = Field(default=180, ge=40, le=1000)
    obstacle_margin: float = Field(default=24, ge=4, le=200)
    lane_gap: float = Field(default=24, ge=4, le=120)
    reroute_connectors: bool = True
    preserve_positions: bool = False
    include_hidden: bool = False


class LayoutBounds(StrictModel):
    min_x: float = 0
    min_y: float = 0
    max_x: float = 0
    max_y: float = 0
    width: float = 0
    height: float = 0


class AutoLayoutMetrics(StrictModel):
    node_count: int = Field(default=0, ge=0)
    connector_count: int = Field(default=0, ge=0)
    overlaps_before: int = Field(default=0, ge=0)
    overlaps_after: int = Field(default=0, ge=0)
    pipe_obstacle_intersections_before: int = Field(default=0, ge=0)
    pipe_obstacle_intersections_after: int = Field(default=0, ge=0)
    shared_lane_segments_before: int = Field(default=0, ge=0)
    shared_lane_segments_after: int = Field(default=0, ge=0)
    total_route_length_before: float = Field(default=0, ge=0)
    total_route_length_after: float = Field(default=0, ge=0)
    duplicate_label_count_before: int = Field(default=0, ge=0)
    duplicate_label_count_after: int = Field(default=0, ge=0)
    text_text_overlaps_before: int = Field(default=0, ge=0)
    text_text_overlaps_after: int = Field(default=0, ge=0)
    text_symbol_overlaps_before: int = Field(default=0, ge=0)
    text_symbol_overlaps_after: int = Field(default=0, ge=0)
    text_connector_intersections_before: int = Field(default=0, ge=0)
    text_connector_intersections_after: int = Field(default=0, ge=0)
    bounds_before: LayoutBounds = Field(default_factory=LayoutBounds)
    bounds_after: LayoutBounds = Field(default_factory=LayoutBounds)


class AutoLayoutPreview(StrictModel):
    valid: bool = True
    document_id: str
    current_revision: int = Field(ge=0)
    transaction: TransactionRequest | None = None
    moved_element_ids: list[str] = Field(default_factory=list)
    rerouted_connector_ids: list[str] = Field(default_factory=list)
    moved_annotation_ids: list[str] = Field(default_factory=list)
    skipped_locked_element_ids: list[str] = Field(default_factory=list)
    locked_element_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: AutoLayoutMetrics = Field(default_factory=AutoLayoutMetrics)
