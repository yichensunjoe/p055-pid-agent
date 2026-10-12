"""DI-1-D3: the frozen scenario corpus is a locked measurement instrument.

The corpus (backend/tests/di1_corpus/corpus.json) was frozen by the Gate's DI-1-D2 Design
Gate (2026-10-12, S1–S6). These tests make drift loud: any edit to the corpus changes the
pinned digest and fails here, so evidence produced from a drifted corpus can never silently
pass as evidence for the frozen one (the M8-Q1 corpus-lock discipline).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CORPUS_PATH = Path(__file__).parent / "di1_corpus" / "corpus.json"

#: Pinned identity of the frozen corpus (sha256 of the exact file bytes).
CORPUS_SHA256 = "5a2450d9946acf765bf9ed70acefb381c68f5495aba5d70b9742124351893742"

FROZEN_SCENARIO_IDS = ["S1", "S2", "S3", "S4", "S5", "S6"]
FROZEN_CATEGORIES = ["简单流程线", "设备+仪表", "控制回路", "管线连接", "复杂局部"]
FROZEN_RECORD_FIELDS = ["输入描述", "期望结果", "实际生成结果", "人工评价", "replay 结果"]


def _load() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def test_corpus_file_is_pinned() -> None:
    digest = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
    assert digest == CORPUS_SHA256, (
        "di1 corpus drifted: the frozen scenario set may not change without a Gate ruling; "
        f"file sha256 {digest} != pinned {CORPUS_SHA256}"
    )


def test_corpus_structure_is_complete() -> None:
    corpus = _load()
    assert corpus["corpus_id"] == "di1-corpus"
    assert corpus["version"] == 1
    assert corpus["evidence_record_fields"] == FROZEN_RECORD_FIELDS
    assert corpus["categories_required"] == FROZEN_CATEGORIES

    scenarios = corpus["scenarios"]
    assert [s["id"] for s in scenarios] == FROZEN_SCENARIO_IDS
    for scenario in scenarios:
        assert scenario["title"] and scenario["category"]
        assert scenario["covers"], scenario["id"]
        assert scenario["steps"], scenario["id"]
        for step in scenario["steps"]:
            assert step["kind"] in ("plan", "edit", "expect-receipt"), step
            assert len(step["sentence"]) >= 4, step
        expected = scenario["expected"]
        assert expected["tags"], scenario["id"]
        assert isinstance(expected["connections"], list)
        assert expected["completeness"] == "complete"
        assert isinstance(scenario["gap_links"], list)


def test_the_five_gate_categories_are_all_covered() -> None:
    corpus = _load()
    covered = set()
    for scenario in corpus["scenarios"]:
        covered.update(scenario["covers"])
    for category in FROZEN_CATEGORIES:
        assert category in covered, f"Gate-frozen category {category} has no scenario"


def test_gap_probes_cover_the_frozen_gap_list() -> None:
    corpus = _load()
    probe_gaps = {p["gap"] for p in corpus["gap_probes"]}
    # The D2-frozen gap list: G4 routing / TT-101 symbol / system declaration live as probes;
    # G3 (selectors) and HOLDOUT-1 (换成) are exercised inside scenarios S5/S6.
    assert {"G4", "TT-101", "SYSTEM-DECL"} <= probe_gaps
    linked = {gap for s in corpus["scenarios"] for gap in s["gap_links"]}
    assert {"G3", "HOLDOUT-1"} <= linked
    for probe in corpus["gap_probes"]:
        assert probe["title"] and probe["source"] and probe["sentence"]
        assert probe["kind"] in ("plan", "edit")
