"""CLI tests for the deterministic drafting commands (M3).

The pipeline is a CI gate: a drawing that fails the drafting gate exits 2, and the CLI
has no apply flag at all — a drawing is only changed through the governed transaction
channel, so drafting cannot become a side door into the store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad.cli import main
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def _seed(database: Path) -> str:
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="CLI drafting"))
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="pump_a",
                        symbol_key="centrifugal_pump",
                        position=Point(x=100, y=300),
                        width=80,
                        height=70,
                        label="P-101",
                    )
                ),
                AddElementOperation(
                    element=SymbolElement(
                        id="pump_b",
                        symbol_key="centrifugal_pump",
                        position=Point(x=150, y=320),
                        width=80,
                        height=70,
                        label="P-102",
                    )
                ),
            ],
        ),
    )
    return document.id


def _seed_clean(database: Path) -> str:
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="CLI clean"))
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="pump",
                        symbol_key="centrifugal_pump",
                        position=Point(x=120, y=300),
                        width=80,
                        height=70,
                        label="P-201",
                    )
                )
            ],
        ),
    )
    return document.id


def test_drafting_report_exits_zero_for_a_drawing_that_passes_the_gate(tmp_path: Path, capsys):
    database = tmp_path / "cli.db"
    document_id = _seed_clean(database)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "drafting",
                "--database",
                str(database),
                "report",
                document_id,
                "--summary",
            ]
        )

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_id"] == document_id
    assert payload["gate"]["passed"] is True
    assert payload["gate"]["checked_codes"]
    assert payload["port_count"] >= 2


def test_drafting_report_exits_two_when_the_gate_fails(tmp_path: Path, capsys):
    database = tmp_path / "cli-fail.db"
    document_id = _seed(database)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "drafting",
                "--database",
                str(database),
                "report",
                document_id,
                "--summary",
            ]
        )

    assert excinfo.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"]["passed"] is False
    assert (
        payload["findings"]
        or payload["gate"]["blockers"]
        or payload["gate"]["drawing_issues"]
    )


def test_drafting_preview_is_reproducible_and_leaves_the_document_alone(tmp_path: Path, capsys):
    database = tmp_path / "cli-preview.db"
    document_id = _seed(database)

    for _ in range(2):
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "drafting",
                    "--database",
                    str(database),
                    "preview",
                    document_id,
                    "--summary",
                ]
            )
        assert excinfo.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        digest = payload["transaction_digest"]
        assert digest
        assert payload["operation_count"] >= 1
        assert payload["regressions"] == []
        assert payload["locked_element_ids"] == []

    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    assert service.get_document(document_id).revision == 1


def test_drafting_cli_honours_waivers_and_locks(tmp_path: Path, capsys):
    database = tmp_path / "cli-scope.db"
    document_id = _seed(database)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "drafting",
                "--database",
                str(database),
                "preview",
                document_id,
                "--lock",
                "pump_a",
                "--no-relayout",
                "--summary",
            ]
        )

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "pump_a" not in payload["moved_element_ids"]
    assert "pump_a" in payload["locked_element_ids"]


def test_drafting_has_no_apply_flag(tmp_path: Path):
    """Drafting must not grow its own write path: it previews, the channel applies."""

    with pytest.raises(SystemExit) as excinfo:
        main(["drafting", "preview", "any", "--apply"])

    assert excinfo.value.code == 2
