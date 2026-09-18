from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .models import StrictModel

SemanticEntityKind = Literal[
    "equipment",
    "valve",
    "instrument",
    "pipeline",
    "junction",
    "annotation",
    "graphic",
    "layer",
    "system",
    "symbol",
    "unknown",
]
SemanticChangeAction = Literal["added", "deleted", "updated"]
SemanticRiskHint = Literal["draft_edit", "engineering_change", "critical_change"]


class SemanticFieldDelta(StrictModel):
    field: str
    before: Any = None
    after: Any = None
    category: Literal[
        "identity",
        "engineering",
        "connectivity",
        "geometry",
        "routing",
        "style",
        "metadata",
        "other",
    ] = "other"


class SemanticChange(StrictModel):
    entity_kind: SemanticEntityKind
    entity_id: str
    display_name: str
    action: SemanticChangeAction
    change_types: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    field_deltas: list[SemanticFieldDelta] = Field(default_factory=list)
    risk_hint: SemanticRiskHint = "engineering_change"
    summary: str
    symbol_key: str | None = None


class SemanticDiffReport(StrictModel):
    schema: Literal["pid-agent.semantic-diff"] = "pid-agent.semantic-diff"
    version: Literal[1] = 1
    document_id: str
    base_revision: int = Field(ge=0)
    result_revision: int = Field(ge=0)
    change_count: int = Field(ge=0)
    engineering_change_count: int = Field(default=0, ge=0)
    critical_change_count: int = Field(default=0, ge=0)
    draft_edit_count: int = Field(default=0, ge=0)
    truncated: bool = False
    changes: list[SemanticChange] = Field(default_factory=list)
