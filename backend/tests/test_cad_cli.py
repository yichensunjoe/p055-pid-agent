"""CLI tests for ``pid-agent import-cad``.

The CLI is the surface an unattended job uses, so the behaviours that matter are the
exit codes: 0 when the import happened (or was deliberately a dry run), 2 when it was
refused, and a machine-readable report on stdout either way.
"""

from __future__ import annotations

import json

import pytest
from cad_fixtures import DxfBuilder, simple_dxf

from agentcad.cli import main
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


@pytest.fixture()
def database(tmp_path) -> str:
    return str(tmp_path / "cad-cli.db")


def _run(argv: list[str]) -> str:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 0
    return ""


def test_capabilities_are_printed_without_a_file(capsys, database: str) -> None:
    main(["import-cad", "--capabilities", "--database", database])

    payload = json.loads(capsys.readouterr().out)
    assert payload["dxf_import"] is True
    assert any(item["key"] == "libredwg-object-stream" for item in payload["converters"])


def test_a_dry_run_creates_no_document(capsys, database: str, tmp_path) -> None:
    path = tmp_path / "drawing.dxf"
    path.write_bytes(simple_dxf())

    main(["import-cad", str(path), "--dry-run", "--summary", "--database", database])

    payload = json.loads(capsys.readouterr().out)
    assert payload["elements"] > 0
    assert payload["transactions"] == 0
    assert "document_id" not in payload, "a dry run must not claim to have created one"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    assert service.list_documents() == []


def test_an_import_creates_a_document_and_prints_its_report(
    capsys, database: str, tmp_path
) -> None:
    path = tmp_path / "drawing.dxf"
    path.write_bytes(simple_dxf())

    main(["import-cad", str(path), "--name", "气路系统总图", "--summary", "--database", database])

    payload = json.loads(capsys.readouterr().out)
    assert payload["document_name"] == "气路系统总图"
    assert payload["revision"] > 0
    assert payload["counts"]["elements"] > 0
    assert payload["source"]["filename"] == "drawing.dxf"

    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    documents = service.list_documents()
    assert len(documents) == 1
    assert documents[0].name == "气路系统总图"


def test_the_cli_import_is_attributed_to_the_cli_surface(
    capsys, database: str, tmp_path
) -> None:
    path = tmp_path / "drawing.dxf"
    path.write_bytes(simple_dxf())
    main(["import-cad", str(path), "--summary", "--database", database])
    payload = json.loads(capsys.readouterr().out)

    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    trail = service.audit.audit_trail(document_id=payload["document_id"], limit=20)
    revisions = [record for record in trail if record.event_type == "revision.created"]
    assert revisions
    assert all(record.actor == "cli" for record in revisions)
    assert all(record.surface == "cli" for record in revisions)


def test_options_are_honoured_from_the_command_line(capsys, database: str, tmp_path) -> None:
    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.layer("EQUIP", color=5)
    builder.line((0.0, 0.0), (100.0, 0.0), layer="PIPE")
    builder.text("P-101", (0.0, 0.0), 5.0, layer="EQUIP")
    builder.hatch([[(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)]], solid=True, layer="PIPE")
    path = tmp_path / "drawing.dxf"
    path.write_bytes(builder.build())

    main(
        [
            "import-cad",
            str(path),
            "--layer",
            "PIPE",
            "--no-text",
            "--no-fills",
            "--unit-scale",
            "10",
            "--frame",
            "-1",
            "-1",
            "1000",
            "1000",
            "--summary",
            "--database",
            database,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["texts"] == 0
    assert payload["counts"]["fills"] == 0
    assert [layer for layer in payload["layers"] if layer != "0"] == ["PIPE"]
    assert payload["canvas"]["width"] == pytest.approx(100.1)


def test_output_file_receives_the_same_json(capsys, database: str, tmp_path) -> None:
    path = tmp_path / "drawing.dxf"
    path.write_bytes(simple_dxf())
    report = tmp_path / "report.json"

    main(["import-cad", str(path), "--summary", "--output", str(report), "--database", database])

    stdout_payload = json.loads(capsys.readouterr().out)
    assert json.loads(report.read_text(encoding="utf-8")) == stdout_payload


def test_a_refused_import_exits_two_with_a_code(capsys, database: str, tmp_path) -> None:
    path = tmp_path / "not-a-drawing.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["import-cad", str(path), "--database", database])

    assert excinfo.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "unrecognised_format"
    assert payload["retryable"] is False


def test_a_missing_file_exits_two_with_a_code(capsys, database: str, tmp_path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["import-cad", str(tmp_path / "nope.dxf"), "--database", database])

    assert excinfo.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "source_not_found"


def test_the_element_limit_can_be_set_and_refuses_politely(
    capsys, database: str, tmp_path
) -> None:
    builder = DxfBuilder()
    for index in range(12):
        builder.line((0.0, float(index)), (5.0, float(index)))
    path = tmp_path / "many.dxf"
    path.write_bytes(builder.build())

    with pytest.raises(SystemExit) as excinfo:
        main(["import-cad", str(path), "--max-elements", "5", "--database", database])

    assert excinfo.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "too_many_elements"
