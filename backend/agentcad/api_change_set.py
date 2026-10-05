"""M13-D5: change-set evidence / read surface (Gate CODE GO).

Frozen scope: GET-only, zero-audit exposure of the durable change-set facts —
status, intent, impacted snapshot, preview snapshot, result pins and
post-commit evidence. No write endpoints, no new editor, no new approval
states. Writes stay exclusively on the D4 governed runtime path.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .store import SQLiteDocumentStore


def _parse_json_column(raw: object, fallback: object) -> object:
    if raw is None:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


def create_change_set_router(store: SQLiteDocumentStore) -> APIRouter:
    router = APIRouter(
        prefix="/api/v2/projects/{project_id}/change-sets",
        tags=["P&ID-Agent change sets"],
    )

    def _row_payload(row: dict) -> dict:
        return {
            "change_set_id": str(row["change_set_id"]),
            "project_id": str(row["project_id"]),
            "status": str(row["status"]),
            "base_pins": _parse_json_column(row["base_pins"], {}),
            "intent": _parse_json_column(row["intent"], {}),
            "intent_hash": str(row["intent_hash"]),
            "impacted": _parse_json_column(row["impacted"], {}),
            "preview": _parse_json_column(row["preview"], {}),
            "result_pins": _parse_json_column(row["result_pins"], {}),
            "evidence": _parse_json_column(row["evidence"], {}),
            "session_id": row["session_id"],
            "approval_id": row["approval_id"],
            "tool_call_id": row["tool_call_id"],
            "created_at": str(row["created_at"]),
            "created_by": str(row["created_by"]),
            "updated_at": str(row["updated_at"]),
        }

    @router.get("")
    def list_change_sets(project_id: str) -> dict:
        if store.get_project(project_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "project_not_found", "message": f"project {project_id!r} not found"},
            )
        return {
            "project_id": project_id,
            "change_sets": [
                _row_payload(row) for row in store.list_change_sets(project_id)
            ],
        }

    @router.get("/{change_set_id}")
    def change_set_detail(project_id: str, change_set_id: str) -> dict:
        if store.get_project(project_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "project_not_found", "message": f"project {project_id!r} not found"},
            )
        row = store.get_change_set(change_set_id)
        if row is None or str(row["project_id"]) != project_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "change_set_not_found",
                    "message": f"change set {change_set_id!r} not found",
                },
            )
        return _row_payload(row)

    return router
