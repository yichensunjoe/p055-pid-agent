"""M13-D5: change-set governed write path (HTTP stage/apply).

Hard locks: stage runs the SEALED D3 analyzer + shadow previewer and
persists via D2 primitives — zero engineering writes (pins unchanged);
apply routes through the full M10/D4 governed runtime (session -> approval
-> authorization -> atomic executor) with no copied domain logic; local
deployment may auto-resolve the operator approval, shared mode refuses it
(approval_not_self_served, fail-closed).
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from test_m13_d4_executor import AS_OF, DEFAULT_PROJECT, Plane

from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.project_change_set import change_set_intent_hash
from agentcad.project_readiness import ProjectReadinessService


def _client(tmp_path: Path, name: str, plane: Plane, mode: str) -> TestClient:
    settings = Settings(
        database_path=tmp_path / name,
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode=mode,  # type: ignore[arg-type]
        api_token="deployment-token" if mode == "shared" else None,
        operator_identity="李工",
    )
    return TestClient(create_app(settings))


def _stage_payload(plane: Plane) -> dict:
    intent = plane.intent()
    payload = intent.model_dump(mode="json")
    payload.pop("project_id")
    return payload


def test_stage_runs_shadow_preview_with_zero_engineering_writes(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "stage.db")
    pins = plane.pins()
    client = _client(tmp_path, "stage.db", plane, "local")

    response = client.post(
        f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets",
        json=_stage_payload(plane),
    )
    assert response.status_code == 200
    body = response.json()
    change_set_id = body["change_set_id"]
    assert body["status"] == "staged"
    assert body["impacted"]["affected_documents"]
    assert body["preview"]["intent_hash"] == change_set_intent_hash(plane.intent())

    # the shadow preview must not have moved any engineering fact
    assert plane.pins() == pins
    link = plane.store.get_engineering_link("lnk_seed")
    assert link["pinned_source_revision"] == pins[plane.cable_id]

    # exactly one governance-plane staged audit, chain intact
    staged_events = [
        e
        for e in plane.recorder.store.all_audit_records()
        if e.event_type == "project_change_set.staged"
    ]
    assert len(staged_events) == 1
    assert staged_events[0].project_id == DEFAULT_PROJECT
    assert staged_events[0].evidence["zero_engineering_writes"] is True
    assert plane.recorder.verify_chain().ok

    detail = client.get(f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets/{change_set_id}").json()
    assert detail["status"] == "staged"
    assert detail["preview"]["intent_hash"]
    assert detail["result_pins"] == {}
    assert detail["created_by"] == "web-user"


def test_apply_local_full_governed_flow(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "apply.db")
    pins = plane.pins()
    client = _client(tmp_path, "apply.db", plane, "local")

    change_set_id = client.post(
        f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets",
        json=_stage_payload(plane),
    ).json()["change_set_id"]

    applied = client.post(f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets/{change_set_id}/apply")
    assert applied.status_code == 200
    result_pins = applied.json()["result_pins"]
    assert result_pins == {doc: pin + 1 for doc, pin in pins.items()}

    detail = client.get(f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets/{change_set_id}").json()
    assert detail["status"] == "applied"
    assert detail["result_pins"] == result_pins
    assert detail["approval_id"] and detail["session_id"] and detail["tool_call_id"]
    evidence = detail["evidence"]
    assert evidence["before_pins"] == pins
    assert evidence["after_pins"] == result_pins
    assert evidence["audit_record_ids"]

    # post-commit readiness hash equals the frozen in-transaction snapshot
    post = ProjectReadinessService(plane.store, plane.service, plane.cable).assess(
        project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF
    )
    assert evidence["readiness_result_hash"] == post.result_hash

    # governance chain survived the endpoint-driven flow
    assert plane.recorder.verify_chain().ok


def test_apply_twice_is_rejected_after_terminal_closeout(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "twice.db")
    client = _client(tmp_path, "twice.db", plane, "local")
    change_set_id = client.post(
        f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets",
        json=_stage_payload(plane),
    ).json()["change_set_id"]
    assert (
        client.post(
            f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets/{change_set_id}/apply"
        ).status_code
        == 200
    )

    second = client.post(f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets/{change_set_id}/apply")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "change_set_not_staged"


def test_stage_invalid_intent_is_422(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "invalid.db")
    client = _client(tmp_path, "invalid.db", plane, "local")
    response = client.post(
        f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets",
        json={"base_member_pins": {}, "mutations": []},  # missing evaluation_as_of
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_intent"


def test_apply_fail_closed_in_shared_mode(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "shared.db")
    headers = {"Authorization": "Bearer deployment-token"}
    client = _client(tmp_path, "shared.db", plane, "shared")

    staged = client.post(
        f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets",
        json=_stage_payload(plane),
        headers=headers,
    )
    assert staged.status_code == 200
    change_set_id = staged.json()["change_set_id"]

    refused = client.post(
        f"/api/v2/projects/{DEFAULT_PROJECT}/change-sets/{change_set_id}/apply",
        headers=headers,
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "approval_not_self_served"
    # staging a row never mutated engineering state
    row = plane.store.get_change_set(change_set_id)
    assert row["status"] == "staged"
    assert json.loads(row["result_pins"]) == {}
