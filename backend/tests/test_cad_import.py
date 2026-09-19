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

import hashlib
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
from agentcad.service import DocumentService, InvalidOperationError
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


def test_an_import_is_one_logical_mutation_in_history_and_audit(
    service: DocumentService, importer: CadImporter
) -> None:
    """M4-0.2: the whole drawing is one revision, one history entry, one audit record."""

    result = _import(importer, simple_dxf())
    trail = service.audit.audit_trail(document_id=result.document_id, limit=20)
    revision_records = [record for record in trail if record.event_type == "revision.created"]

    assert len(revision_records) == 1, "one import must not be several audit records"
    assert all(record.actor == "test" for record in revision_records)
    history = service.get_history(result.document_id, limit=50)
    assert len(history) == 1
    assert history[0].action == "create"
    assert history[0].revision == result.revision
    assert history[0].operation_count == result.report.operations
    assert result.report.logical_mutations == 1
    assert result.report.revisions == 1
    assert result.revision == 1


def test_audit_evidence_binds_the_source_sha256(
    service: DocumentService, importer: CadImporter
) -> None:
    """M4-0.3: the SHA binding must be in the *audit record*, not only in metadata.

    Exact values, because "provenance was recorded" is only true if the record carries
    the same digest as the bytes that were imported.
    """

    data = simple_dxf()
    result = _import(importer, data, "气路系统总图.dxf")
    trail = service.audit.audit_trail(document_id=result.document_id, limit=20)
    record = next(record for record in trail if record.event_type == "revision.created")
    binding = record.evidence["cad_import"]

    assert binding["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert binding["source_sha256"] == result.report.source.sha256
    assert binding["source_format"] == "dxf"
    assert binding["source_size_bytes"] == len(data)
    assert binding["operation_count"] == result.report.operations
    assert record.document_id == result.document_id
    assert record.result_revision == result.revision


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
    assert plan.report.logical_mutations == 0
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


def test_a_large_import_is_one_logical_action_with_one_undo(
    service: DocumentService, importer: CadImporter
) -> None:
    """M4-0.2: >1000 operations, one history entry, one undo, one redo.

    The former implementation wrote this as several transactions, so one undo removed
    only the last batch and left the rest of the drawing behind.
    """

    builder = DxfBuilder()
    for index in range(1200):
        builder.line((0.0, float(index)), (10.0, float(index)))
    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    assert result.report.operations > 1000
    assert len(document.elements) > 1000
    assert result.report.logical_mutations == 1
    history = service.get_history(result.document_id, limit=50)
    assert len(history) == 1
    assert history[0].operation_count == result.report.operations

    undone = service.undo(result.document_id, expected_revision=document.revision)
    assert undone.elements == [], "one undo must reverse the whole import"

    redone = service.redo(undone.id, expected_revision=undone.revision)
    assert len(redone.elements) == len(document.elements)
    assert [element.id for element in redone.elements] == [
        element.id for element in document.elements
    ]


def test_a_failure_part_way_through_leaves_nothing_behind(
    service: DocumentService, importer: CadImporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4-0.2: no half-imported drawing may ever be readable as a normal document."""

    builder = DxfBuilder()
    for index in range(30):
        builder.line((0.0, float(index)), (10.0, float(index)))
    original = DocumentService._apply_operation
    calls = {"count": 0}

    def failing(self, document, operation):
        calls["count"] += 1
        if calls["count"] == 12:
            raise InvalidOperationError("injected failure while applying operation 12")
        return original(self, document, operation)

    monkeypatch.setattr(DocumentService, "_apply_operation", failing)

    with pytest.raises(CadImportError) as excinfo:
        _import(importer, builder.build())

    assert excinfo.value.code == "import_rejected"
    assert calls["count"] == 12
    assert service.list_documents() == [], "a failed import must not leave a document"


def test_the_bulk_create_path_shares_the_mutation_kernel(
    service: DocumentService,
) -> None:
    """M4-0 hardening: "one governed path" is a code fact, not a claim.

    The same operations go in through both entry points - the ordinary transaction and
    the bulk create CAD import uses. Operation application, editor-group normalization,
    the revision convention, the resulting-document validation and the audit evidence
    must all agree, because a policy hook attached to only one of them is exactly the
    failure mode the remote review flagged.
    """

    from copy import deepcopy

    from agentcad.models import (
        AddElementOperation,
        AddLayerOperation,
        Layer,
        Point,
        SymbolElement,
        TransactionRequest,
    )

    def build_operations() -> list:
        return [
            AddLayerOperation(layer=Layer(id="layer_process", name="Process")),
            AddElementOperation(
                element=SymbolElement(
                    id="sym_bulk_a",
                    symbol_key="gate_valve",
                    label="HV-1",
                    position=Point(x=10, y=10),
                    width=30,
                    height=30,
                )
            ),
            AddElementOperation(
                element=SymbolElement(
                    id="sym_bulk_b",
                    symbol_key="gate_valve",
                    label="HV-2",
                    position=Point(x=90, y=10),
                    width=30,
                    height=30,
                )
            ),
        ]

    created = service.create_document(CreateDocumentRequest(name="via transaction"))
    via_transaction = service.apply_transaction(
        created.id,
        TransactionRequest(
            expected_revision=created.revision,
            operations=deepcopy(build_operations()),
            label="fixture",
        ),
    ).document
    via_bulk = service.create_document_with_operations(
        CreateDocumentRequest(name="via bulk create"),
        operations=build_operations(),
        label="fixture",
    ).document

    # Same operations => same document, element for element. This is what fails if the
    # bulk path ever grows its own application or normalization step.
    assert [element.model_dump(mode="json") for element in via_bulk.elements] == [
        element.model_dump(mode="json") for element in via_transaction.elements
    ]
    assert [layer.model_dump(mode="json") for layer in via_bulk.layers] == [
        layer.model_dump(mode="json") for layer in via_transaction.layers
    ]
    assert via_bulk.revision == via_transaction.revision == 1
    assert via_bulk.metadata == via_transaction.metadata

    # Both paths record exactly one *mutation* history entry for the operation list. The
    # bulk path folds the creation into it (the document arrives with content); the
    # ordinary path necessarily has its earlier `create` entry first.
    bulk_history = service.get_history(via_bulk.id, limit=10)
    transaction_history = service.get_history(via_transaction.id, limit=10)

    # ``get_history`` returns the newest entry first.
    assert [entry.action for entry in bulk_history] == ["create"]
    assert [entry.action for entry in transaction_history] == ["transaction", "create"]
    assert bulk_history[0].operation_count == 3
    assert transaction_history[0].operation_count == 3

    # Same provenance builder: the evidence a reviewer reads must have the same shape on
    # both paths, otherwise one of them has a hook the other does not.
    def evidence_for(document_id: str) -> dict:
        records = service.audit.audit_trail(document_id=document_id)
        revision_records = [record for record in records if record.event_type == "revision.created"]
        assert len(revision_records) == 1
        return revision_records[0].evidence

    bulk_evidence = evidence_for(via_bulk.id)
    transaction_evidence = evidence_for(via_transaction.id)

    assert sorted(bulk_evidence) == sorted(transaction_evidence)
    assert bulk_evidence["element_count_after"] == transaction_evidence["element_count_after"]
    assert bulk_evidence["change_count"] == transaction_evidence["change_count"]
    assert bulk_evidence["operation_count"] == transaction_evidence["operation_count"] == 3
    assert bulk_evidence["validation"] == transaction_evidence["validation"]


def test_the_bulk_create_path_validates_and_leaves_nothing_on_failure(
    service: DocumentService,
) -> None:
    """The shared kernel's rejection behavior applies to bulk create too."""

    from agentcad.models import (
        AddElementOperation,
        Point,
        SymbolElement,
        TransactionRequest,
    )

    duplicate = AddElementOperation(
        element=SymbolElement(
            id="sym_dup",
            symbol_key="gate_valve",
            label="HV-9",
            position=Point(x=1, y=1),
            width=10,
            height=10,
        )
    )
    add_twice = [duplicate, duplicate.model_copy(deep=True)]

    created = service.create_document(CreateDocumentRequest(name="transaction target"))
    with pytest.raises(InvalidOperationError):
        service.apply_transaction(
            created.id,
            TransactionRequest(
                expected_revision=created.revision, operations=add_twice, label="dup"
            ),
        )
    assert service.get_document(created.id).elements == []

    with pytest.raises(InvalidOperationError):
        service.create_document_with_operations(
            CreateDocumentRequest(name="bulk target"),
            operations=add_twice,
            label="dup",
        )
    assert [document.name for document in service.list_documents()] == ["transaction target"]


def test_a_non_uniform_block_scale_turns_a_circle_into_a_sampled_polyline(
    service: DocumentService, importer: CadImporter
) -> None:
    """M4-0.7: a circle under ``scale_x != scale_y`` is an ellipse, not a circle."""

    circle_entity: list = [
        (0, "CIRCLE"),
        (8, "0"),
        (10, 0.0),
        (20, 0.0),
        (30, 0.0),
        (40, 5.0),
    ]
    builder = DxfBuilder()
    builder.block("ELLIPTIC", [circle_entity])
    builder.insert("ELLIPTIC", (0.0, 0.0), scale=(3.0, 1.0))

    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)
    element = next(item for item in document.elements if item.type == "polyline")

    assert [item.type for item in document.elements] == ["polyline"]
    assert element.metadata["cad_block"] == "ELLIPTIC"
    assert any(issue.code == "CAD_CIRCLE_APPROXIMATED" for issue in result.report.issues)


def test_a_uniformly_scaled_circle_stays_a_circle(
    service: DocumentService, importer: CadImporter
) -> None:
    """The control for M4-0.7: only a non-uniform transform changes the primitive."""

    circle_entity: list = [
        (0, "CIRCLE"),
        (8, "0"),
        (10, 0.0),
        (20, 0.0),
        (30, 0.0),
        (40, 5.0),
    ]
    builder = DxfBuilder()
    builder.block("ROUND", [circle_entity])
    builder.insert("ROUND", (0.0, 0.0), scale=(2.0, 2.0))

    result = _import(importer, builder.build())
    document = service.get_document(result.document_id)

    assert [item.type for item in document.elements] == ["circle"]
    assert not any(issue.code == "CAD_CIRCLE_APPROXIMATED" for issue in result.report.issues)


def test_dry_run_path_reports_a_missing_file_like_a_real_import(
    importer: CadImporter, tmp_path
) -> None:
    """M4-0.8: dry run and write mode share one loader, so they share its error codes."""

    with pytest.raises(CadImportError) as excinfo:
        importer.dry_run_path(tmp_path / "missing.dxf")

    assert excinfo.value.code == "source_not_found"


def test_dry_run_path_refuses_a_directory(importer: CadImporter, tmp_path) -> None:
    with pytest.raises(CadImportError) as excinfo:
        importer.dry_run_path(tmp_path)

    assert excinfo.value.code == "source_not_a_file"


def test_report_is_json_serialisable(importer: CadImporter) -> None:
    """The report crosses HTTP, MCP and the CLI, so it must round-trip as JSON."""

    result = _import(importer, simple_dxf())
    payload = json.loads(result.model_dump_json())

    assert payload["report"]["counts"]["elements"] == result.report.counts.elements
    assert payload["report"]["source"]["sha256"] == result.report.source.sha256
    assert payload["document_id"] == result.document_id
