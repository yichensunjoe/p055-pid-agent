"""REST surface tests for CAD import.

The interesting questions are not "does it return 201": they are *where the write lands*
(one new document through the governed channel, with an audit record), *whether the dry
run is really dry*, and whether a refusal comes back with a code and a usable message
instead of a stack trace. The upload is the raw request body, so these tests also pin
that contract — including that the file name and options arrive as query parameters.
"""

from __future__ import annotations

import pytest
from cad_fixtures import DxfBuilder, simple_dxf
from fastapi.testclient import TestClient

from agentcad import cad_convert
from agentcad.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "cad-api.db"))
    app = create_app()
    with TestClient(app) as test_client:
        test_client.app_state = app.state  # type: ignore[attr-defined]
        yield test_client


def _import(client: TestClient, data: bytes, filename: str = "drawing.dxf", **params):
    return client.post(
        "/api/v2/imports/cad",
        params={"filename": filename, **params},
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def test_capabilities_route_reports_the_decoders(client: TestClient) -> None:
    response = client.get("/api/v2/imports/cad/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dxf_import"] is True
    assert "dxf" in payload["formats"]
    assert any(item["key"] == "libredwg-object-stream" for item in payload["converters"])
    assert payload["max_source_bytes"] > 0


def test_capabilities_route_writes_nothing(client: TestClient) -> None:
    before = len(client.get("/api/v2/documents").json())
    client.get("/api/v2/imports/cad/capabilities")
    client.get("/api/v2/imports/cad/capabilities")

    assert len(client.get("/api/v2/documents").json()) == before


def test_import_creates_one_document_and_reports_what_it_did(client: TestClient) -> None:
    documents_before = client.get("/api/v2/documents").json()

    response = _import(client, simple_dxf(), "气路系统总图.dxf")

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["document_name"] == "气路系统总图 (CAD 导入)"
    assert payload["report"]["counts"]["elements"] > 0
    assert payload["report"]["source"]["filename"] == "气路系统总图.dxf"
    assert payload["report"]["source"]["format"] == "dxf"
    assert len(payload["report"]["source"]["sha256"]) == 64
    assert len(payload["report"]["layers"]) >= 3

    documents_after = client.get("/api/v2/documents").json()
    assert len(documents_after) == len(documents_before) + 1
    document = client.get(f"/api/v2/documents/{payload['document_id']}").json()
    assert len(document["elements"]) == payload["report"]["counts"]["elements"]


def test_import_leaves_an_audited_revision(client: TestClient) -> None:
    response = _import(client, simple_dxf())
    document_id = response.json()["document_id"]
    service = client.app_state.service  # type: ignore[attr-defined]

    trail = service.audit.audit_trail(document_id=document_id, limit=20)
    revision_records = [record for record in trail if record.event_type == "revision.created"]
    assert revision_records
    assert all(record.surface == "rest" for record in revision_records)
    assert all(record.tool_name == "import_cad_drawing" for record in revision_records)


def test_import_does_not_touch_existing_documents(client: TestClient) -> None:
    existing = client.post("/api/v2/documents", json={"name": "Keep me"}).json()

    _import(client, simple_dxf())

    current = client.get(f"/api/v2/documents/{existing['id']}").json()
    assert current["revision"] == existing["revision"]
    assert current["elements"] == []


def test_plan_route_decodes_without_writing(client: TestClient) -> None:
    before = client.get("/api/v2/documents").json()

    response = client.post(
        "/api/v2/imports/cad/plan",
        params={"filename": "drawing.dxf"},
        content=simple_dxf(),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["elements"] > 0
    assert payload["operations"] >= payload["elements"]
    assert payload["report"]["logical_mutations"] == 0
    assert client.get("/api/v2/documents").json() == before


def test_options_arrive_as_query_parameters(client: TestClient) -> None:
    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.layer("EQUIP", color=5)
    builder.line((0.0, 0.0), (10.0, 0.0), layer="PIPE")
    builder.circle((5.0, 5.0), 1.0, layer="EQUIP")
    builder.text("P-101", (0.0, 0.0), 2.0, layer="EQUIP")

    response = _import(client, builder.build(), name="Cropped import", layers="PIPE", include_text=False)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["document_name"] == "Cropped import"
    assert payload["report"]["counts"]["texts"] == 0
    assert [layer for layer in payload["report"]["layers"] if layer != "0"] == ["PIPE"]


def test_frame_parameter_is_parsed_and_applied(client: TestClient) -> None:
    builder = DxfBuilder()
    builder.line((0.0, 0.0), (10.0, 0.0))
    builder.line((1000.0, 0.0), (1010.0, 0.0))

    response = _import(client, builder.build(), frame="-1,-1,50,50")

    assert response.status_code == 201, response.text
    assert response.json()["report"]["counts"]["elements"] == 1


def test_an_invalid_frame_is_a_client_error_with_a_code(client: TestClient) -> None:
    response = _import(client, simple_dxf(), frame="1,2,3")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_frame"
    assert detail["retryable"] is False


def test_an_unreadable_body_is_refused_with_a_code(client: TestClient) -> None:
    response = _import(client, b"this is not a drawing")

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unrecognised_format"


def test_an_empty_body_is_refused_with_a_code(client: TestClient) -> None:
    """An empty upload is a malformed request (400), not an unreadable drawing (422)."""

    response = _import(client, b"")

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "empty_source"


def test_a_dwg_without_an_installed_decoder_is_refused_with_guidance(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "empty-bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(cad_convert, "EXTRA_APPLICATION_GLOBS", ())
    # The declared application globs are neutralized too, so an installed AutoCAD cannot
    # change what these tests observe.
    monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})

    response = _import(client, b"AC1032" + b"\x00" * 64, "drawing.dwg")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "no_dwg_converter"
    assert "DXF" in detail["message"]


def test_oversized_uploads_are_stopped_by_the_request_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "cad-api-limit.db"))
    monkeypatch.setenv("PID_AGENT_MAX_IMPORT_BODY_BYTES", "2048")
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/imports/cad",
            params={"filename": "big.dxf"},
            content=b"0\nSECTION\n" + b"x" * 4096,
        )

    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "request_body_too_large"


def test_the_file_name_defaults_when_it_is_not_supplied(client: TestClient) -> None:
    response = client.post("/api/v2/imports/cad", content=simple_dxf())

    assert response.status_code == 201, response.text
    assert response.json()["report"]["source"]["filename"] == "drawing"


def test_the_import_tool_is_published_in_the_agent_catalog(client: TestClient) -> None:
    catalog = client.get("/api/v2/agent/tools").json()
    tools = {tool["name"]: tool for tool in catalog["tools"]}

    assert "import_cad_drawing" in tools
    definition = tools["import_cad_drawing"]
    assert definition["has_side_effect"] is True
    assert definition["risk"] == "draft_edit"
    assert definition["audit_event"] == "tool.import_cad_drawing"


def test_an_imported_document_renders_and_exports(client: TestClient) -> None:
    """An imported drawing must behave like any other document downstream."""

    response = _import(client, simple_dxf())
    document_id = response.json()["document_id"]

    svg = client.get(f"/api/v2/documents/{document_id}/export.svg")
    assert svg.status_code == 200
    assert "svg" in svg.text[:200]

    dxf = client.get(f"/api/v2/documents/{document_id}/export-v2.dxf")
    assert dxf.status_code == 200
    assert "SECTION" in dxf.text
