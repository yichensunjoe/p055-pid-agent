import json

import pytest

from agentcad.cli import main
from agentcad.quality_harness import run_quality_harness
from agentcad.symbols import SymbolRegistry


def test_offline_quality_harness_passes_without_provider():
    report = run_quality_harness(SymbolRegistry())

    assert report.passed is True
    assert report.total_cases == 7
    assert report.passed_cases == 7
    assert report.failed_cases == 0
    assert [case.name for case in report.cases] == [
        "symbol_catalog_integrity",
        "atomic_topology_transaction",
        "semantic_agent_output_contract",
        "drafting_quality_contract",
        "engineering_graph_contract",
        "deterministic_drafting_contract",
        "cad_import_contract",
    ]
    graph_case = report.cases[4]
    # The M2 case must actually derive engineering objects, not just run.
    assert graph_case.details["object_count"] >= 4
    assert graph_case.details["line_count"] == 1
    assert graph_case.details["error_findings"] == 1  # the deliberate duplicate tag
    semantic = report.cases[2]
    assert semantic.details["junction_degree"] == 3
    assert semantic.details["rejected_issue_codes"] == ["unknown_port"]
    drafting_engine = report.cases[5]
    # The M3 case must really move geometry, honour a lock and improve the drawing.
    assert drafting_engine.details["operation_count"] >= 1
    assert drafting_engine.details["transaction_digest"]
    assert drafting_engine.details["moved_element_ids"]
    assert "draft_locked" in drafting_engine.details["locked_element_ids"]
    assert drafting_engine.details["regressions"] == []
    assert drafting_engine.details["score_after"] > drafting_engine.details["score_before"]
    assert (
        drafting_engine.details["reserved_region_intrusions_after"]
        < drafting_engine.details["reserved_region_intrusions_before"]
    )
    cad_import = report.cases[6]
    # The CAD case must really reproduce a drawing, keep its layer names, admit what it
    # could not reproduce, and refuse an unreadable source instead of guessing.
    assert cad_import.details["element_count"] >= 10
    assert cad_import.details["counts"]["fills"] == 1
    assert {"PIPE", "仪表"} <= set(cad_import.details["layer_names"])
    assert "CAD_PATTERN_HATCH_SKIPPED" in cad_import.details["issue_codes"]
    assert "CAD_UNSUPPORTED_ENTITIES" in cad_import.details["issue_codes"]
    assert cad_import.details["dry_run_transactions"] == 0
    assert cad_import.details["cropped_canvas"] == {"width": 250.0, "height": 150.0}


def test_catalog_harness_accepts_a_valid_dynamic_symbol(tmp_path):
    symbol_file = tmp_path / "extension.json"
    symbol_file.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "key": "quality_note",
                        "name": "质量备注",
                        "category": "标注",
                        "description": "不连接管线的标准图纸备注符号。",
                        "width": 80,
                        "height": 30,
                        "ports": [],
                        "shapes": [
                            {
                                "type": "rect",
                                "x": 0,
                                "y": 0,
                                "width": 80,
                                "height": 30,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_quality_harness(SymbolRegistry(search_paths=[symbol_file]))

    assert report.passed is True
    assert report.symbol_count == len(SymbolRegistry().list()) + 1


def test_catalog_harness_reports_actionable_dynamic_symbol_errors(tmp_path):
    symbol_file = tmp_path / "invalid.json"
    symbol_file.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "key": "bad symbol",
                        "name": "坏图例",
                        "category": "测试",
                        "description": "",
                        "width": 20,
                        "height": 20,
                        "ports": [
                            {
                                "id": "P 1",
                                "name": "",
                                "x": 30,
                                "y": 10,
                                "direction": "in",
                                "medium": "",
                            },
                            {
                                "id": "P 1",
                                "name": "重复端口",
                                "x": 0,
                                "y": 10,
                                "direction": "out",
                                "medium": "process",
                            },
                        ],
                        "shapes": [{"type": "unsupported"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = run_quality_harness(SymbolRegistry(search_paths=[symbol_file]))
    catalog = report.cases[0]
    codes = {finding.code for finding in catalog.findings}

    assert report.passed is False
    assert catalog.status == "failed"
    assert {
        "SYMBOL_KEY_INVALID",
        "SYMBOL_DESCRIPTION_MISSING",
        "SYMBOL_PORT_ID_DUPLICATE",
        "SYMBOL_PORT_ID_INVALID",
        "SYMBOL_PORT_NAME_MISSING",
        "SYMBOL_PORT_MEDIUM_MISSING",
        "SYMBOL_PORT_OUT_OF_BOUNDS",
        "SYMBOL_SHAPE_TYPE_UNSUPPORTED",
    }.issubset(codes)


def test_quality_harness_cli_writes_report(tmp_path, capsys):
    output = tmp_path / "quality-harness.json"

    with pytest.raises(SystemExit) as exited:
        main(["quality-harness", "--output", str(output)])

    assert exited.value.code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "pid-agent.quality-harness"
    assert payload["passed"] is True
    assert json.loads(capsys.readouterr().out)["passed"] is True


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    [
        ('{"symbols": [}', "SYMBOL_FILE_JSON_INVALID"),
        (
            json.dumps(
                {
                    "symbols": [
                        {
                            "key": "invalid_schema",
                            "name": "坏图例",
                            "category": "测试",
                            "description": "宽度不合法。",
                            "width": 0,
                            "height": 20,
                            "ports": [],
                            "shapes": [],
                        }
                    ]
                }
            ),
            "SYMBOL_FILE_SCHEMA_INVALID",
        ),
    ],
)
def test_quality_harness_cli_reports_symbol_load_errors_as_json(
    tmp_path,
    capsys,
    contents,
    expected_code,
):
    symbol_file = tmp_path / "broken.json"
    output = tmp_path / "failed-report.json"
    symbol_file.write_text(contents, encoding="utf-8")

    with pytest.raises(SystemExit) as exited:
        main(
            [
                "quality-harness",
                "--symbol-path",
                str(symbol_file),
                "--output",
                str(output),
            ]
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exited.value.code == 2
    assert captured.err == ""
    assert payload["schema"] == "pid-agent.quality-harness"
    assert payload["passed"] is False
    assert payload["cases"][0]["name"] == "symbol_catalog_load"
    assert payload["cases"][0]["findings"][0]["code"] == expected_code
    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_quality_harness_cli_reports_single_file_duplicate_key(tmp_path, capsys):
    symbol_file = tmp_path / "duplicate.json"
    symbol = {
        "key": "duplicate_cli_fixture",
        "name": "重复图例",
        "category": "测试",
        "description": "CLI 重复 key 验证。",
        "width": 20,
        "height": 20,
        "ports": [],
        "shapes": [{"type": "line", "x1": 0, "y1": 10, "x2": 20, "y2": 10}],
    }
    symbol_file.write_text(
        json.dumps({"symbols": [symbol, symbol]}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exited:
        main(["quality-harness", "--symbol-path", str(symbol_file)])

    payload = json.loads(capsys.readouterr().out)
    finding = payload["cases"][0]["findings"][0]
    assert exited.value.code == 2
    assert finding["code"] == "SYMBOL_FILE_DUPLICATE_KEY"
    assert finding["symbol_key"] == "duplicate_cli_fixture"
