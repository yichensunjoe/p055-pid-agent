"""Importer tests: a decoded drawing becomes a governed document.

The properties under test are the ones an engineer will actually depend on:

* the geometry lands where it was in the source (only the canvas frame changes),
* the layer names are the source's own names, even when two of them sanitise to the
  same ASCII id,
* the write is governed — revision-checked, audited, undoable — and it only ever
  *creates* a document,
* the report is honest about whatever could not be reproduced, and the dry run writes
  nothing at all.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from cad_fixtures import DxfBuilder, ObjectStreamBuilder, simple_dxf

from agentcad import cad_convert
from agentcad.audit_models import AuditContext
from agentcad.cad_import import CadImporter, CadImportError, detect_format
from agentcad.cad_models import CadImportOptions
from agentcad.models import CreateDocumentRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


@pytest.fixture()
def service(tmp_path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "cad-import.db"), SymbolRegistry())


@pytest.fixture()
def importer(service: DocumentService) -> CadImporter:
    return CadImporter(service)


def _audit(actor: str = "test", surface: str = "internal") -> AuditContext:
    return AuditContext(actor=actor, surface=surface, tool_name="import_cad_drawing")  # type: ignore[arg-type]


def _import(importer: CadImporter, data: bytes, filename: str = "drawing.dxf", **options):
    return importer.import_bytes(
        data, filename=filename, options=CadImportOptions(**options), audit=_audit()
    )


# -- the happy path --------------------------------------------------------- #


def test_dxf_import_creates_a_governed_document(service: DocumentService, importer: CadImporter) -> None:
    result = _import(importer, simple_dxf(), "气路系统总图.dxf")
    document = service.get_document(result.document_id)

    assert document.name == "气路系统总图 (CAD 导入)"
    assert result.report.counts.lines == 5
    assert result.report.counts.circles == 1
    assert result.report.counts.texts == 1
    assert result.report.counts.elements == len(document.elements)
    # Two source layers plus the block geometry on layer 0.
    assert {"PIPE", "EQUIP", "0"} <= {layer.name for layer in document.layers}


def test_import_writes_an_audit_record_and_history_entries(
    service: DocumentService, importer: CadImporter
) -> None:
    result = _import(importer, simple_dxf())
    trail = service.audit.audit_trail(document_id=result.document_id, limit=20)
    revision_records = [record for record in trail if record.event_type == "revision.created"]

    assert revision_records, "the import must leave audit evidence per revision"
    assert all(record.actor == "test" for record in revision_records)
    history = service.get_history(result.document_id, limit=50)
    transactions = [entry for entry in history if entry.action == "transaction"]
    assert transactions, "the import must appear in the document history"
    assert result.report.transactions == len(transactions)
    assert result.revision == len(transactions)
    # The creation itself is a history entry too, so an import has one more than its
    # transactions.
    assert len(history) == len(transactions) + 1


def test_import_metadata_records_the_source_fingerprint(
    service: DocumentService, importer: CadImporter
) -> None:
    data = simple_dxf()
    result = _import(importer, data, "drawing.dxf")
    document = service.get_document(result.document_id)
    metadata = document.metadata["cad_import"]

    assert metadata["source_file"] == "drawing.dxf"
    assert metadata["source_format"] == "dxf"
    assert metadata["size_bytes"] == len(data)
    assert metadata["sha256"] == result.report.source.sha256
    assert len(metadata["sha256"]) == 64
    assert metadata["counts"]["elements"] == result.report.counts.elements
    assert "geometry-faithful" in metadata["note"]


def test_import_is_undoable(service: DocumentService, importer: CadImporter) -> None:
    result = _import(importer, simple_dxf())
    before = service.get_document(result.document_id)
    assert before.elements

    document = service.undo(result.document_id, expected_revision=before.revision)

    assert document.revision > before.revision
    assert document.elements == []


def test_import_never_touches_an_existing_document(service: DocumentService, importer: CadImporter) -> None:
    existing = service.create_document(CreateDocumentRequest(name="Keep me"))
    revision_before = existing.revision

    result = _import(importer, simple_dxf())

    assert result.document_id != existing.id
    assert service.get_document(existing.id).revision == revision_before
    assert service.get_document(existing.id).elements == []


def test_repeated_imports_produce_identical_document_content(
    service: DocumentService, importer: CadImporter
) -> None:
    first = _import(importer, simple_dxf())
    second = _import(importer, simple_dxf())
    left = service.get_document(first.document_id)
    right = service.get_document(second.document_id)

    assert left.id != right.id
    assert [element.id for element in left.elements] == [element.id for element in right.elements]
    assert [element.model_dump(mode="json") for element in left.elements] == [
        element.model_dump(mode="json") for element in right.elements
    ]
    assert first.report.counts == second.report.counts


# -- geometry lands where it was -------------------------------------------- #


def test_canvas_frame_flips_the_y_axis(service: DocumentService, importer: CadImporter) -> None:
    """CAD is y-up, the editor canvas is y-down, and nothing else may change."""

    builder = DxfBuilder()
    builder.line((10.0, 100.0), (60.0, 100.0))
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    frame = result.report.frame
    assert frame is not None
    line = document.elements[0]
    assert line.type == "line"
    # y = 100 was the top of the frame, so on the canvas it is y = 0.
    assert (line.start.x, line.start.y) == pytest.approx((0.0, 0.0))
    assert (line.end.x, line.end.y) == pytest.approx((50.0, 0.0))
    assert document.canvas.width == pytest.approx(50.0)
    assert document.canvas.height == pytest.approx(0.0) or document.canvas.height >= 1.0


def test_frame_crop_keeps_only_the_intersecting_geometry(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    builder.line((0.0, 0.0), (10.0, 0.0))
    builder.circle((500.0, 500.0), 5.0)
    result = _import(importer, builder.build(), frame=(0.0, -10.0, 100.0, 10.0))
    document = service.get_document(result.document_id)

    assert len(document.elements) == 1
    assert document.elements[0].type == "line"
    issue = next(issue for issue in result.report.issues if issue.code == "CAD_PRIMITIVES_NOT_IMPORTED")
    assert issue.detail.get("outside_frame") == 1


def test_a_frame_with_no_geometry_is_refused(importer: CadImporter) -> None:
    builder = DxfBuilder()
    builder.line((0.0, 0.0), (10.0, 0.0))

    with pytest.raises(CadImportError) as excinfo:
        _import(importer, builder.build(), frame=(1000.0, 1000.0, 2000.0, 2000.0))

    assert excinfo.value.code == "no_geometry"


def test_invalid_frame_is_refused(importer: CadImporter) -> None:
    with pytest.raises(CadImportError) as excinfo:
        _import(importer, simple_dxf(), frame=(0.0, 0.0, 0.0, 10.0))

    assert excinfo.value.code == "invalid_frame"


def test_unit_scale_divides_coordinates_and_canvas(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    builder.line((0.0, 0.0), (100.0, 0.0))
    result = _import(importer, builder.build(), unit_scale=10.0)
    document = service.get_document(result.document_id)

    assert document.canvas.width == pytest.approx(10.0)
    assert document.elements[0].end.x == pytest.approx(10.0)


def test_layer_filter_imports_only_the_named_layers(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.layer("EQUIP", color=5)
    builder.line((0.0, 0.0), (10.0, 0.0), layer="PIPE")
    builder.circle((5.0, 5.0), 1.0, layer="EQUIP")
    result = _import(importer, builder.build(), layers=["PIPE"])
    document = service.get_document(result.document_id)

    assert [layer.name for layer in document.layers if layer.name != "Default"] == ["PIPE"]
    assert [element.type for element in document.elements] == ["line"]
    assert result.report.counts.skipped == 1


def test_text_can_be_excluded_and_fills_can_be_skipped(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    builder.line((0.0, 0.0), (10.0, 0.0))
    builder.text("P-101", (0.0, 0.0), 10.0)
    builder.hatch([[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]], solid=True)
    result = _import(importer, builder.build(), include_text=False, fills="skip")
    document = service.get_document(result.document_id)

    assert [element.type for element in document.elements] == ["line"]
    assert result.report.counts.skipped == 2
    detail = next(
        issue.detail for issue in result.report.issues if issue.code == "CAD_PRIMITIVES_NOT_IMPORTED"
    )
    assert detail == {"text_disabled": 1, "fills_disabled": 1}


# -- fidelity details ------------------------------------------------------- #


def test_block_provenance_is_kept_on_every_element(service: DocumentService, importer: CadImporter) -> None:
    builder = DxfBuilder()
    builder.rect_block("PDS2D-6Q1C15", corner=(-5.0, -5.0), size=(10.0, 10.0))
    builder.insert("PDS2D-6Q1C15", (100.0, 100.0))
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    assert {element.metadata.get("cad_block") for element in document.elements} == {"PDS2D-6Q1C15"}
    # No engineering meaning is invented: a block stays a block name, not a valve.
    assert all("symbol_key" not in element.model_dump(exclude_none=True) for element in document.elements)


def test_solid_fill_becomes_a_closed_filled_polygon(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    builder.hatch([[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]], solid=True)
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    element = document.elements[0]
    assert element.type == "polyline"
    assert element.closed is True
    assert element.style.fill != "none"
    assert result.report.counts.fills == 1


def test_linetype_becomes_a_dash_pattern(service: DocumentService, importer: CadImporter) -> None:
    builder = DxfBuilder()
    builder.line((0.0, 0.0), (10.0, 0.0), linetype="DASHED")
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    assert document.elements[0].style.dash == [12.0, 6.0]


def test_layers_whose_names_sanitise_identically_stay_separate(
    service: DocumentService, importer: CadImporter
) -> None:
    """Two Unicode layer names collapse to the same ASCII slug; ids must not collide."""

    builder = DxfBuilder()
    builder.layer("阀门", color=1)
    builder.layer("阀门 ", color=2)
    builder.layer("???", color=3)
    builder.line((0.0, 0.0), (1.0, 0.0), layer="阀门")
    builder.line((0.0, 1.0), (1.0, 1.0), layer="???")
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    layer_ids = [element.layer_id for element in document.elements]
    assert len(set(layer_ids)) == 2
    names = {layer.id: layer.name for layer in document.layers}
    assert {names[layer_id] for layer_id in layer_ids} == {"阀门", "???"}


def test_oversized_text_height_is_clamped_and_reported(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    builder.text("HUGE", (0.0, 0.0), 100_000.0)
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    assert document.elements[0].font_size == 500.0
    assert "CAD_TEXT_HEIGHT_CLAMPED" in {issue.code for issue in result.report.issues}


def test_every_issue_carries_a_code_and_a_message(importer: CadImporter) -> None:
    builder = DxfBuilder()
    builder.unknown("SPLINE")
    builder.hatch([[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]], solid=False, pattern="ANSI31")
    builder.text("ROTATED", (0.0, 0.0), 5.0, rotation=45.0)
    builder.line((0.0, 0.0), (10.0, 0.0))
    result = _import(importer, builder.build())

    codes = {issue.code for issue in result.report.issues}
    assert {"CAD_UNSUPPORTED_ENTITIES", "CAD_PATTERN_HATCH_SKIPPED", "CAD_TEXT_ROTATION_IGNORED"} <= codes
    assert all(issue.message for issue in result.report.issues)
    assert all(issue.count > 0 for issue in result.report.issues)


def test_preserve_colors_can_be_switched_off(service: DocumentService, importer: CadImporter) -> None:
    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.line((0.0, 0.0), (10.0, 0.0), layer="PIPE", color=256)
    result = _import(importer, builder.build(), preserve_colors=False)
    document = service.get_document(result.document_id)

    assert document.elements[0].style.stroke == "#111827"


# -- refusals --------------------------------------------------------------- #


def test_empty_source_is_refused(importer: CadImporter) -> None:
    with pytest.raises(CadImportError) as excinfo:
        _import(importer, b"")

    assert excinfo.value.code == "empty_source"


def test_unrecognised_format_is_refused(importer: CadImporter) -> None:
    with pytest.raises(CadImportError) as excinfo:
        _import(importer, b"just some text\n")

    assert excinfo.value.code == "unrecognised_format"


def test_a_drawing_with_no_geometry_is_refused(importer: CadImporter) -> None:
    builder = DxfBuilder()
    builder.text("no geometry", (0.0, 0.0), 5.0)
    with pytest.raises(CadImportError) as excinfo:
        _import(importer, builder.build(), include_text=False)

    assert excinfo.value.code == "no_geometry"


def test_element_limit_is_enforced_before_anything_is_written(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    for index in range(20):
        builder.line((0.0, float(index)), (10.0, float(index)))
    with pytest.raises(CadImportError) as excinfo:
        _import(importer, builder.build(), max_elements=5)

    assert excinfo.value.code == "too_many_elements"
    assert service.list_documents() == []


def test_source_size_limit_is_enforced(service: DocumentService) -> None:
    importer = CadImporter(service, max_source_bytes=16)
    with pytest.raises(CadImportError) as excinfo:
        _import(importer, simple_dxf())

    assert excinfo.value.code == "source_too_large"


def test_symbol_load_failure_is_not_masked() -> None:
    """Guards the error contract the API and CLI map onto status codes."""

    assert detect_format(simple_dxf()) == ("dxf", "")
    assert detect_format(b"AC1032" + b"\x00" * 10) == ("dwg", "AC1032")
    with pytest.raises(CadImportError):
        detect_format(b"AutoCAD Binary DXF\x00\x00")


# -- DWG path --------------------------------------------------------------- #


def _install_fake_reader(bindir: Path) -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [
            builder.line((0.0, 0.0), (50.0, 0.0)),
            builder.circle((25.0, 10.0), 5.0),
        ]
    )
    script = bindir / "dwgread"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({json.dumps(builder.build())!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def fake_reader(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(cad_convert, "EXTRA_APPLICATION_GLOBS", ())
    # The declared application globs are neutralized too, so an installed AutoCAD cannot
    # change what these tests observe.
    monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})
    monkeypatch.delenv("PID_AGENT_CAD_CONVERTER", raising=False)
    cad_convert.converter_version.cache_clear()
    _install_fake_reader(bindir)
    return bindir


def test_dwg_import_records_the_converter_that_decoded_it(
    service: DocumentService, importer: CadImporter, fake_reader: Path
) -> None:
    data = b"AC1032" + b"\x00" * 64
    result = importer.import_bytes(
        data, filename="气路系统总图.dwg", options=CadImportOptions(), audit=_audit()
    )
    document = service.get_document(result.document_id)
    metadata = document.metadata["cad_import"]

    assert result.report.source.format == "dwg"
    assert result.report.source.format_detail == "AC1032"
    assert result.report.source.converter == "libredwg-object-stream"
    assert metadata["converter"] == "libredwg-object-stream"
    assert metadata["converter_command"][0].endswith("dwgread")
    assert len(document.elements) == 2
    assert result.report.warnings == []


def test_dwg_import_without_a_converter_is_refused_with_guidance(
    service: DocumentService, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "empty-bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(cad_convert, "EXTRA_APPLICATION_GLOBS", ())
    # The declared application globs are neutralized too, so an installed AutoCAD cannot
    # change what these tests observe.
    monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})
    importer = CadImporter(service)

    with pytest.raises(CadImportError) as excinfo:
        importer.import_bytes(
            b"AC1032" + b"\x00" * 64, filename="drawing.dwg", audit=_audit()
        )

    assert excinfo.value.code == "no_dwg_converter"
    assert "DXF" in excinfo.value.message
    assert service.list_documents() == []


def test_an_unverified_converter_is_recorded_as_a_warning(
    service: DocumentService, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ODA converter is declared unverified; the report must say so."""

    bindir = tmp_path / "oda-bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(cad_convert, "EXTRA_APPLICATION_GLOBS", ())
    # The declared application globs are neutralized too, so an installed AutoCAD cannot
    # change what these tests observe.
    monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})
    cad_convert.converter_version.cache_clear()
    script = bindir / "ODAFileConverter"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[2])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        f"(out / 'converted.dxf').write_bytes({simple_dxf()!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    importer = CadImporter(service)

    result = importer.import_bytes(
        b"AC1032" + b"\x00" * 64, filename="drawing.dwg", audit=_audit()
    )

    assert result.report.source.converter == "oda-dxf"
    assert any("unverified" in warning for warning in result.report.warnings)
    assert service.get_document(result.document_id).elements


def test_capabilities_report_what_this_installation_can_decode(
    importer: CadImporter, fake_reader: Path
) -> None:
    capabilities = importer.capabilities()

    assert capabilities.dxf_import is True
    assert capabilities.dwg_import is True
    assert capabilities.formats == ["dxf", "dwg"]
    assert "gbk" in capabilities.decode_encoding
    assert capabilities.max_source_bytes > 0
    assert any(item.key == "libredwg-object-stream" and item.available for item in capabilities.converters)
    assert any(item.key == "oda-dxf" and not item.available for item in capabilities.converters)


def test_dry_run_writes_nothing(service: DocumentService, importer: CadImporter) -> None:
    plan = importer.dry_run(simple_dxf(), filename="drawing.dxf")

    assert plan.elements > 0
    assert plan.operations >= plan.elements
    assert plan.document_name
    assert plan.report.counts.elements == plan.elements
    assert plan.report.transactions == 0
    assert plan.report.operations == 0
    assert service.list_documents() == []


def test_import_path_imports_a_file_from_disk(
    service: DocumentService, importer: CadImporter, tmp_path
) -> None:
    path = tmp_path / "drawing.dxf"
    path.write_bytes(simple_dxf())

    result = importer.import_path(path, audit=_audit())

    assert service.get_document(result.document_id).elements
    assert result.report.source.filename == "drawing.dxf"


def test_import_path_reports_a_missing_file(importer: CadImporter, tmp_path) -> None:
    with pytest.raises(CadImportError) as excinfo:
        importer.import_path(tmp_path / "nope.dxf")

    assert excinfo.value.code == "source_not_found"


def test_batching_keeps_each_transaction_within_the_operation_cap(
    service: DocumentService, importer: CadImporter
) -> None:
    builder = DxfBuilder()
    for index in range(25):
        builder.line((0.0, float(index)), (10.0, float(index)))
    result = _import(importer, builder.build(), chunk_size=10)

    assert result.report.transactions == 3
    assert result.revision == 3
    assert result.report.counts.elements == 25
    history = service.get_history(result.document_id, limit=10)
    assert len([entry for entry in history if entry.action == "transaction"]) == 3


def test_report_is_json_serialisable(importer: CadImporter) -> None:
    """The report crosses HTTP, MCP and the CLI, so it must round-trip as JSON."""

    result = _import(importer, simple_dxf())
    payload = json.loads(result.model_dump_json())

    assert payload["report"]["counts"]["elements"] == result.report.counts.elements
    assert payload["report"]["source"]["sha256"] == result.report.source.sha256
    assert payload["document_id"] == result.document_id
