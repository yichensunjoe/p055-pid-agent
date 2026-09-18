from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .models import Document, TransactionRequest
from .semantic_diff_models import (
    SemanticChange,
    SemanticDiffReport,
    SemanticFieldDelta,
)
from .service import InvalidOperationError, RevisionConflictError
from .symbols import SymbolRegistry

_GEOMETRY_FIELDS = {
    "position",
    "x",
    "y",
    "width",
    "height",
    "rotation",
    "center",
    "radius",
}
_ROUTING_FIELDS = {
    "points",
    "routing",
    "crossing_style",
    "jump_radius",
    "arrow_position",
}
_CONNECTIVITY_FIELDS = {"source", "target", "port_id", "element_id"}
_ENGINEERING_FIELDS = {
    "symbol_key",
    "label",
    "process_tag",
    "medium",
    "nominal_diameter",
    "flow_direction",
    "properties",
    "name",
}
_STYLE_FIELDS = {"style", "font_size", "anchor"}
_METADATA_FIELDS = {"metadata", "layer_id", "system_id"}

_CRITICAL_SYMBOL_KEYS = {
    "safety_relief_valve",
    "relief_valve",
    "pressure_safety_valve",
}


def _field_category(field: str) -> str:
    if field in _CONNECTIVITY_FIELDS:
        return "connectivity"
    if field in _ROUTING_FIELDS:
        return "routing"
    if field in _GEOMETRY_FIELDS:
        return "geometry"
    if field in _ENGINEERING_FIELDS:
        return "engineering"
    if field in _STYLE_FIELDS:
        return "style"
    if field in _METADATA_FIELDS:
        return "metadata"
    if field in {"id", "type"}:
        return "identity"
    return "other"


def _symbol_kind(symbol_key: str, symbols: SymbolRegistry | None) -> str:
    lowered_key = symbol_key.casefold()
    category = ""
    if symbols is not None:
        try:
            category = symbols.get(symbol_key).category.casefold()
        except KeyError:
            category = ""

    if "valve" in lowered_key or "阀" in category or "valve" in category:
        return "valve"
    if (
        "instrument" in lowered_key
        or "transmitter" in lowered_key
        or "indicator" in lowered_key
        or "controller" in lowered_key
        or "仪表" in category
        or "instrument" in category
    ):
        return "instrument"
    if (
        "equipment" in category
        or "设备" in category
        or any(
            token in lowered_key
            for token in (
                "pump",
                "tank",
                "vessel",
                "compressor",
                "exchanger",
                "reactor",
                "column",
                "filter",
                "separator",
            )
        )
    ):
        return "equipment"
    return "symbol"


def _entity_kind(payload: dict[str, Any] | None, fallback_kind: str, symbols: SymbolRegistry | None) -> str:
    if fallback_kind in {"layer", "system"}:
        return fallback_kind
    if not payload:
        return "unknown"
    element_type = str(payload.get("type", ""))
    if element_type == "connector":
        return "pipeline"
    if element_type == "junction":
        return "junction"
    if element_type == "text":
        return "annotation"
    if element_type == "symbol":
        return _symbol_kind(str(payload.get("symbol_key", "")), symbols)
    if element_type in {"line", "polyline", "rectangle", "circle"}:
        return "graphic"
    return "unknown"


def _display_name(payload: dict[str, Any] | None, entity_id: str, kind: str) -> str:
    if not payload:
        return entity_id
    for key in ("label", "process_tag", "name", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            normalized = " ".join(value.strip().split())
            return normalized[:120]
    if kind == "pipeline":
        medium = payload.get("medium")
        if isinstance(medium, str) and medium.strip():
            return f"{entity_id} ({medium.strip()})"
    return entity_id


def _change_types(
    action: str,
    kind: str,
    changed_fields: list[str],
) -> list[str]:
    if action == "added":
        return ["created"]
    if action == "deleted":
        return ["removed"]

    fields = set(changed_fields)
    result: list[str] = []
    if fields & _CONNECTIVITY_FIELDS:
        result.append("connectivity_changed")
    if kind == "pipeline" and fields & _ROUTING_FIELDS:
        result.append("rerouted")
    elif fields & (_GEOMETRY_FIELDS | _ROUTING_FIELDS):
        result.append("layout_changed")
    if fields & _ENGINEERING_FIELDS:
        result.append("engineering_properties_changed")
    if fields & _STYLE_FIELDS:
        result.append("style_changed")
    if fields & _METADATA_FIELDS:
        result.append("metadata_changed")
    return result or ["updated"]


def _risk_hint(
    action: str,
    kind: str,
    symbol_key: str | None,
    changed_fields: list[str],
) -> str:
    if symbol_key in _CRITICAL_SYMBOL_KEYS and action in {"deleted", "updated"}:
        return "critical_change"

    fields = set(changed_fields)
    if action == "updated":
        engineering = fields & (_ENGINEERING_FIELDS | _CONNECTIVITY_FIELDS)
        if not engineering and fields <= (_GEOMETRY_FIELDS | _ROUTING_FIELDS | _STYLE_FIELDS | _METADATA_FIELDS):
            return "draft_edit"

    if kind in {"annotation", "graphic", "layer", "system"}:
        if action == "updated":
            return "draft_edit"
        if kind in {"annotation", "graphic"}:
            return "draft_edit"

    return "engineering_change"


def _summary(
    action: str,
    kind: str,
    display_name: str,
    change_types: list[str],
    changed_fields: list[str],
) -> str:
    noun = kind.replace("_", " ")
    if action == "added":
        return f"Added {noun} {display_name}"
    if action == "deleted":
        return f"Deleted {noun} {display_name}"
    if change_types == ["rerouted"]:
        return f"Rerouted {noun} {display_name}"
    fields = ", ".join(changed_fields[:8])
    suffix = f": {fields}" if fields else ""
    return f"Updated {noun} {display_name}{suffix}"


def semantic_diff_from_history_details(
    document_id: str,
    details: dict[str, Any],
    symbols: SymbolRegistry | None = None,
) -> SemanticDiffReport:
    changes: list[SemanticChange] = []
    for raw in details.get("changes", []):
        if not isinstance(raw, dict):
            continue
        before = raw.get("before") if isinstance(raw.get("before"), dict) else None
        after = raw.get("after") if isinstance(raw.get("after"), dict) else None
        payload = after or before
        entity_id = str(raw.get("entity_id", ""))
        fallback_kind = str(raw.get("entity_kind", "unknown"))
        kind = _entity_kind(payload, fallback_kind, symbols)
        action = str(raw.get("change", "updated"))
        if action not in {"added", "deleted", "updated"}:
            action = "updated"
        changed_fields = [
            str(item)
            for item in raw.get("changed_fields", [])
            if isinstance(item, str)
        ]
        display_name = _display_name(payload, entity_id, kind)
        symbol_key = (
            str(payload.get("symbol_key"))
            if payload and isinstance(payload.get("symbol_key"), str)
            else None
        )
        change_types = _change_types(action, kind, changed_fields)
        deltas = [
            SemanticFieldDelta(
                field=field,
                before=before.get(field) if before else None,
                after=after.get(field) if after else None,
                category=_field_category(field),
            )
            for field in changed_fields
            if field not in {"updated_at", "created_at"}
        ]
        risk = _risk_hint(action, kind, symbol_key, changed_fields)
        changes.append(
            SemanticChange(
                entity_kind=kind,
                entity_id=entity_id,
                display_name=display_name,
                action=action,
                change_types=change_types,
                changed_fields=changed_fields,
                field_deltas=deltas,
                risk_hint=risk,
                summary=_summary(action, kind, display_name, change_types, changed_fields),
                symbol_key=symbol_key,
            )
        )

    engineering_count = sum(item.risk_hint == "engineering_change" for item in changes)
    critical_count = sum(item.risk_hint == "critical_change" for item in changes)
    draft_count = sum(item.risk_hint == "draft_edit" for item in changes)
    return SemanticDiffReport(
        document_id=document_id,
        base_revision=max(0, int(details.get("base_revision", 0))),
        result_revision=max(0, int(details.get("result_revision", 0))),
        change_count=len(changes),
        engineering_change_count=engineering_count,
        critical_change_count=critical_count,
        draft_edit_count=draft_count,
        truncated=bool(details.get("diff_truncated", False)),
        changes=changes,
    )


def build_semantic_diff(
    before: Document,
    after: Document,
    history_details: dict[str, Any],
    symbols: SymbolRegistry | None = None,
) -> SemanticDiffReport:
    if before.id != after.id:
        raise ValueError("semantic diff requires the same document identity")
    return semantic_diff_from_history_details(before.id, history_details, symbols)


def preview_transaction_semantic_diff(
    service,
    document_id: str,
    request: TransactionRequest,
) -> SemanticDiffReport:
    current = service.get_document(document_id)
    if request.expected_revision is not None and request.expected_revision != current.revision:
        raise RevisionConflictError(
            f"expected revision {request.expected_revision}, current revision is {current.revision}"
        )

    working = Document.model_validate(current.model_dump(mode="python"))
    for index, operation in enumerate(request.operations):
        try:
            service._apply_operation(working, operation)
        except InvalidOperationError as exc:
            raise InvalidOperationError(
                f"operations[{index}] ({operation.op}): {exc}"
            ) from exc

    working.revision = current.revision + 1
    try:
        working = Document.model_validate(working.model_dump(mode="python"))
    except ValidationError as exc:
        raise InvalidOperationError(f"resulting document is invalid: {exc}") from exc

    from .history_diff import build_history_details

    details = build_history_details(
        current,
        working,
        request.operations,
        action="preview",
    )
    return build_semantic_diff(current, working, details, service.symbols)
