from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .audit import request_audit_context
from .audit_models import AuditContext
from .legacy_models import (
    LegacyBatchRequest,
    LegacyCircleRequest,
    LegacyCreateLayerRequest,
    LegacyLineRequest,
    LegacyPolylineRequest,
    LegacyRectangleRequest,
    LegacySymbolRequest,
    LegacyTextRequest,
)
from .models import (
    CircleElement,
    ClearDocumentOperation,
    Document,
    Layer,
    LineElement,
    Point,
    PolylineElement,
    RectangleElement,
    Style,
    SymbolElement,
    TextElement,
    TransactionRequest,
    new_id,
)
from .service import DocumentNotFoundError, DocumentService
from .store import StoredDocument
from .svg import render_svg


def create_v1_compat_router(service: DocumentService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Legacy compatibility"])
    legacy_id = "doc_legacy"

    def legacy_context(tool_name: str, label: str) -> AuditContext:
        """Server-derived attribution for the legacy v1 surface.

        The v1 compatibility API is not exempt from the evidence chain: every write it
        performs is recorded as ``surface=rest`` with the legacy endpoint named, so a
        reviewer never sees a v1 edit attributed to ``internal/system``.
        """
        return request_audit_context(
            f"legacy_v1.{tool_name}",
            actor="legacy-v1-client",
            surface="rest",
            label=label,
            metadata={"surface": "legacy_v1"},
        )

    def document() -> Document:
        try:
            return service.get_document(legacy_id)
        except DocumentNotFoundError:
            created = Document(id=legacy_id, name="Legacy Canvas")
            context = legacy_context("get_scene", "Create legacy canvas")
            draft = service.audit.build_event(
                "document.created",
                context,
                document_id=created.id,
                result_revision=created.revision,
                evidence={
                    "name": created.name,
                    "history_source": "web",
                    "implicit": True,
                    "reason": "legacy v1 scene accessed before any document existed",
                },
            )
            service.store.save(
                StoredDocument(document=created, undo_stack=[], redo_stack=[]),
                audit=draft,
            )
            return created

    def layer_operations(current: Document, layer_name: str):
        if not layer_name or layer_name == "default":
            return "layer_default", []
        existing = next((item for item in current.layers if item.name == layer_name), None)
        if existing:
            return existing.id, []
        layer = Layer(id=new_id("layer"), name=layer_name)
        return layer.id, [{"op": "add_layer", "layer": layer}]

    def build_element(raw: dict[str, Any]):
        try:
            return _build_element(raw)
        except HTTPException:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid legacy primitive: {exc}",
            ) from exc

    def _build_element(raw: dict[str, Any]):
        kind = raw.get("type")
        style = Style(
            stroke=str(raw.get("color", "#111827")),
            stroke_width=float(raw.get("linewidth", 1.5)),
        )
        if kind == "line":
            return LineElement(
                start=Point(x=raw["start"][0], y=raw["start"][1]),
                end=Point(x=raw["end"][0], y=raw["end"][1]),
                style=style,
            )
        if kind == "circle":
            return CircleElement(
                center=Point(x=raw["center"][0], y=raw["center"][1]),
                radius=raw["radius"],
                style=style,
            )
        if kind == "rectangle":
            x1, y1, x2, y2 = raw["x1"], raw["y1"], raw["x2"], raw["y2"]
            return RectangleElement(
                x=min(x1, x2),
                y=min(y1, y2),
                width=abs(x2 - x1),
                height=abs(y2 - y1),
                style=style,
            )
        if kind == "polyline":
            return PolylineElement(
                points=[Point(x=item[0], y=item[1]) for item in raw["points"]],
                style=style,
            )
        if kind == "text":
            return TextElement(
                position=Point(x=raw.get("x", 0), y=raw.get("y", 0)),
                text=str(raw.get("content", "")),
                font_size=float(raw.get("font_size", 14)),
                style=style,
            )
        if kind in {"symbol", "industrial_symbol"}:
            key = raw.get("symbol_key") or raw.get("symbol_type")
            try:
                definition = service.symbols.get(str(key))
            except KeyError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return SymbolElement(
                symbol_key=str(key),
                position=Point(x=raw.get("x", 0), y=raw.get("y", 0)),
                width=float(raw.get("width", definition.width)),
                height=float(raw.get("height", definition.height)),
                rotation=float(raw.get("rotation", 0)),
                label=str(raw.get("label", "")),
                style=style,
            )
        raise HTTPException(status_code=422, detail=f"unsupported legacy primitive type: {kind}")

    def add(element, layer_name: str = "default"):
        current = document()
        layer_id, operations = layer_operations(current, layer_name)
        element.layer_id = layer_id
        operations.append({"op": "add_element", "element": element})
        result = service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=operations,
                expected_revision=current.revision,
                label="Legacy API draw",
            ),
            audit=legacy_context("draw", "Legacy API draw"),
        )
        return {
            "success": True,
            "message": "OK",
            "data": element.model_dump(mode="json"),
            "primitives_count": len(result.document.elements),
            "history_size": result.document.revision,
        }

    @router.post("/draw/line")
    def draw_line(request: LegacyLineRequest):
        return add(
            LineElement(
                start=Point(x=request.start[0], y=request.start[1]),
                end=Point(x=request.end[0], y=request.end[1]),
                style=Style(stroke=request.color, stroke_width=request.linewidth),
            ),
            request.layer,
        )

    @router.post("/draw/circle")
    def draw_circle(request: LegacyCircleRequest):
        return add(
            CircleElement(
                center=Point(x=request.center[0], y=request.center[1]),
                radius=request.radius,
                style=Style(stroke=request.color, stroke_width=request.linewidth),
            ),
            request.layer,
        )

    @router.post("/draw/rectangle")
    def draw_rectangle(request: LegacyRectangleRequest):
        return add(
            RectangleElement(
                x=min(request.x1, request.x2),
                y=min(request.y1, request.y2),
                width=abs(request.x2 - request.x1),
                height=abs(request.y2 - request.y1),
                style=Style(stroke=request.color, stroke_width=request.linewidth),
            ),
            request.layer,
        )

    @router.post("/draw/polyline")
    def draw_polyline(request: LegacyPolylineRequest):
        return add(
            PolylineElement(
                points=[Point(x=item[0], y=item[1]) for item in request.points],
                style=Style(stroke=request.color, stroke_width=request.linewidth),
            ),
            request.layer,
        )

    @router.post("/draw/text")
    def draw_text(request: LegacyTextRequest):
        return add(
            TextElement(
                position=Point(x=request.x, y=request.y),
                text=request.content,
                font_size=request.font_size,
                style=Style(stroke=request.color, stroke_width=request.linewidth),
            ),
            request.layer,
        )

    @router.post("/draw/symbol")
    def draw_symbol(request: LegacySymbolRequest):
        try:
            definition = service.symbols.get(request.symbol_type)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return add(
            SymbolElement(
                symbol_key=request.symbol_type,
                position=Point(x=request.x, y=request.y),
                width=request.width or definition.width,
                height=request.height or definition.height,
                rotation=request.rotation,
                label=request.label,
                style=Style(stroke=request.color, stroke_width=request.linewidth),
            ),
            request.layer,
        )

    @router.post("/draw/batch")
    def draw_batch(request: LegacyBatchRequest):
        current = document()
        operations: list[dict[str, Any]] = []
        layer_ids = {layer.name: layer.id for layer in current.layers}
        created = []
        for raw in request.operations:
            layer_name = str(raw.get("layer", "default"))
            if layer_name == "default":
                layer_id = "layer_default"
            elif layer_name in layer_ids:
                layer_id = layer_ids[layer_name]
            else:
                layer = Layer(id=new_id("layer"), name=layer_name)
                layer_ids[layer_name] = layer.id
                layer_id = layer.id
                operations.append({"op": "add_layer", "layer": layer})
            element = build_element(raw)
            element.layer_id = layer_id
            operations.append({"op": "add_element", "element": element})
            created.append(element.model_dump(mode="json"))
        if not operations:
            return {
                "success": True,
                "message": "OK",
                "data": {"created": []},
                "primitives_count": len(current.elements),
                "history_size": current.revision,
            }
        result = service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=operations, expected_revision=current.revision, label="Legacy batch draw"
            ),
            audit=legacy_context("draw_batch", "Legacy batch draw"),
        )
        return {
            "success": True,
            "message": "OK",
            "data": {"created": created},
            "primitives_count": len(result.document.elements),
            "history_size": result.document.revision,
        }

    @router.get("/primitives")
    def primitives():
        current = document()
        return {
            "success": True,
            "message": "OK",
            "data": {"primitives": [item.model_dump(mode="json") for item in current.elements]},
            "primitives_count": len(current.elements),
            "history_size": current.revision,
        }

    @router.get("/primitives/{primitive_id}")
    def get_primitive(primitive_id: str):
        current = document()
        element = next((item for item in current.elements if item.id == primitive_id), None)
        if element is None:
            raise HTTPException(status_code=404, detail=f"primitive not found: {primitive_id}")
        return {"success": True, "message": "OK", "data": element.model_dump(mode="json")}

    @router.delete("/primitives/{primitive_id}")
    def delete_primitive(primitive_id: str):
        current = document()
        element = next((item for item in current.elements if item.id == primitive_id), None)
        if element is None:
            raise HTTPException(status_code=404, detail=f"primitive not found: {primitive_id}")
        result = service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=[{"op": "delete_element", "element_id": primitive_id}],
                expected_revision=current.revision,
                label="Legacy delete",
            ),
            audit=legacy_context("delete_primitive", "Legacy delete"),
        )
        return {
            "success": True,
            "message": "OK",
            "data": {"deleted": element.model_dump(mode="json")},
            "primitives_count": len(result.document.elements),
            "history_size": result.document.revision,
        }

    @router.get("/scene")
    def scene():
        return document()

    @router.delete("/clear")
    def clear():
        current = document()
        result = service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=[ClearDocumentOperation()], expected_revision=current.revision
            ),
            audit=legacy_context("clear", "Legacy clear"),
        )
        return {
            "success": True,
            "primitives_count": 0,
            "history_size": result.document.revision,
        }

    @router.post("/undo")
    def undo():
        current = service.undo(
            document().id, audit=legacy_context("undo", "Legacy undo")
        )
        return {
            "success": True,
            "data": current.model_dump(mode="json"),
            "primitives_count": len(current.elements),
            "history_size": current.revision,
        }

    @router.post("/redo")
    def redo():
        current = service.redo(
            document().id, audit=legacy_context("redo", "Legacy redo")
        )
        return {
            "success": True,
            "data": current.model_dump(mode="json"),
            "primitives_count": len(current.elements),
            "history_size": current.revision,
        }

    @router.get("/layers")
    def list_layers():
        current = document()
        counts = {layer.id: 0 for layer in current.layers}
        for element in current.elements:
            counts[element.layer_id] = counts.get(element.layer_id, 0) + 1
        return {
            "success": True,
            "message": "OK",
            "layers": [
                {
                    "name": layer.name,
                    "visible": layer.visible,
                    "locked": layer.locked,
                    "count": counts.get(layer.id, 0),
                }
                for layer in current.layers
            ],
        }

    @router.post("/layers")
    def create_layer(request: LegacyCreateLayerRequest):
        current = document()
        if any(layer.name == request.name for layer in current.layers):
            raise HTTPException(status_code=400, detail=f"layer already exists: {request.name}")
        service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=[
                    {
                        "op": "add_layer",
                        "layer": Layer(name=request.name, visible=request.visible),
                    }
                ],
                expected_revision=current.revision,
                label="Legacy create layer",
            ),
            audit=legacy_context("create_layer", "Legacy create layer"),
        )
        return list_layers()

    @router.delete("/layers/{name}")
    def delete_layer(name: str):
        current = document()
        layer = next((item for item in current.layers if item.name == name), None)
        if layer is None:
            raise HTTPException(status_code=404, detail=f"layer not found: {name}")
        if layer.id == "layer_default":
            raise HTTPException(status_code=400, detail="default layer cannot be deleted")
        service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=[
                    {
                        "op": "delete_layer",
                        "layer_id": layer.id,
                        "move_elements_to": "layer_default",
                    }
                ],
                expected_revision=current.revision,
                label="Legacy delete layer",
            ),
            audit=legacy_context("delete_layer", "Legacy delete layer"),
        )
        return list_layers()

    @router.patch("/layers/{name}/visibility")
    def toggle_layer_visibility(name: str):
        current = document()
        layer = next((item for item in current.layers if item.name == name), None)
        if layer is None:
            raise HTTPException(status_code=404, detail=f"layer not found: {name}")
        result = service.apply_transaction(
            current.id,
            TransactionRequest(
                operations=[
                    {
                        "op": "update_layer",
                        "layer_id": layer.id,
                        "patch": {"visible": not layer.visible},
                    }
                ],
                expected_revision=current.revision,
                label="Legacy toggle layer",
            ),
            audit=legacy_context("toggle_layer", "Legacy toggle layer"),
        )
        updated = next(item for item in result.document.layers if item.id == layer.id)
        return {"layer": updated.name, "visible": updated.visible}

    @router.get("/export/svg")
    def export_svg():
        current = document()
        return {"svg": render_svg(current, service.symbols), "format": "svg"}

    @router.get("/symbols/library")
    def symbols():
        entries = service.symbols.list()
        categories: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        info: dict[str, dict[str, Any]] = {}
        for symbol in entries:
            categories.setdefault(symbol.category, []).append(symbol.key)
            names[symbol.key] = symbol.name
            info[symbol.key] = {"width": symbol.width, "height": symbol.height}
        return {
            "success": True,
            "message": "OK",
            "data": {"categories": categories, "names": names, "symbols": info},
        }

    return router
