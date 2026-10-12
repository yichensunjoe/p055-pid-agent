"""DI-1-D5 evaluation runner: the five-record evidence per frozen scenario + drawings.

Runs the Gate-frozen corpus (backend/tests/di1_corpus/corpus.json, S1–S6) twice on fresh
server processes (deterministic replay), and for every committed scenario step exports the
drawing as PNG + DXF into reports/di1-d5/ so a human reviewer can judge drawing usability
against the actual pixels. Assembles reports/di1-d5/evaluation.json with the five frozen
record fields per scenario: 输入描述 / 期望结果 / 实际生成结果 / 人工评价 / replay 结果.

The 人工评价 field is written as PENDING_OWNER with the exported drawing paths -- the
machine fills what it can prove, the human fills what only a human can judge.

Usage:  python reports/di1_d5_evaluation.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reports"))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from di1_baseline_runner import (  # noqa: E402
    _check_expected,
    _free_port,
    _run_corpus_once,
    _run_gap_probes,
    _sh,
    _start_server,
)

CORPUS_PATH = REPO_ROOT / "backend" / "tests" / "di1_corpus" / "corpus.json"
OUT_DIR = REPO_ROOT / "reports" / "di1-d5"


def _export(base: str, document_id: str, name: str) -> dict:
    """PNG + DXF exports of the committed drawing, saved for human review."""

    import urllib.request

    out: dict = {}
    for fmt in ("png", "dxf"):
        url = f"{base}/documents/{document_id}/export-v2.{fmt}?range=content&padding=24"
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                raw = resp.read()
                if resp.status == 200:
                    path = OUT_DIR / f"{name}.{fmt}"
                    path.write_bytes(raw)
                    out[fmt] = {"path": str(path.relative_to(REPO_ROOT)), "bytes": len(raw)}
                else:
                    out[fmt] = {"http": resp.status}
        except urllib.error.HTTPError as exc:
            out[fmt] = {"http": exc.code, "error": exc.read().decode(errors="replace")[:200]}
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    runs = []
    exports: dict[str, dict] = {}
    for run_index in (1, 2):
        scratch = Path(tempfile.mkdtemp(prefix=f"di1-d5-{run_index}-"))
        port = _free_port()
        proc = _start_server(port, scratch)
        base = f"http://127.0.0.1:{port}/api/v2"
        print(f"== run {run_index}: port={port}")
        try:
            scenarios = _run_corpus_once(base, corpus)
            probes = _run_gap_probes(base, corpus) if run_index == 1 else []
            if run_index == 1:
                for scenario in corpus["scenarios"]:
                    entry = scenarios[scenario["id"]]
                    committed = [s for s in entry["steps"] if s.get("outcome") == "committed"]
                    if committed:
                        exports[scenario["id"]] = _export(
                            base, entry["document_id"], f"{scenario['id']}"
                        )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        runs.append({"run": run_index, "scenarios": scenarios, "gap_probes": probes})

    # Replay evidence (same discipline as the baseline runner).
    replay = []
    for scenario in corpus["scenarios"]:
        digests = [
            [s.get("spec_digest", "") for s in run["scenarios"][scenario["id"]]["steps"]]
            for run in runs
        ]
        outcomes = [
            [s["outcome"] for s in run["scenarios"][scenario["id"]]["steps"]]
            for run in runs
        ]
        replay.append({
            "scenario": scenario["id"],
            "digest_match": digests[0] == digests[1],
            "outcome_match": outcomes[0] == outcomes[1],
        })

    # The five frozen record fields per scenario.
    evaluations = []
    for scenario in corpus["scenarios"]:
        entry = runs[0]["scenarios"][scenario["id"]]
        check = _check_expected(scenario["expected"], entry["steps"])
        latencies = [s["latency_ms"] for s in entry["steps"] if s.get("latency_ms")]
        walls = [s["wall_ms"] for s in entry["steps"]]
        evaluations.append({
            "scenario": scenario["id"],
            "title": scenario["title"],
            "category": scenario["category"],
            "输入描述": [s["sentence"] for s in scenario["steps"]],
            "期望结果": scenario["expected"],
            "实际生成结果": {
                "outcomes": [s["outcome"] for s in entry["steps"]],
                "semantic_check": check,
                "quality": [s.get("quality") for s in entry["steps"] if s.get("quality")],
                "latency_ms": latencies,
                "wall_ms": walls,
                "drawing_exports": exports.get(scenario["id"], {}),
            },
            "人工评价": {
                "status": "PENDING_OWNER",
                "reviewer": "",
                "verdict": "",
                "note": "看 reports/di1-d5/ 下该场景的 PNG/DXF 导出后填写：是我要的图 / 不是 + 一句话理由",
            },
            "replay 结果": next(r for r in replay if r["scenario"] == scenario["id"]),
        })

    result = {
        "milestone": "DI-1-D5 evaluation (Gate CODE GO 2026-10-12: evaluation + non-regression + closeout)",
        "corpus_sha256": __import__("hashlib").sha256(CORPUS_PATH.read_bytes()).hexdigest(),
        "evaluations": evaluations,
        "gap_probes": runs[0]["gap_probes"],
        "replay_summary": {
            "scenarios": len(replay),
            "both_match": sum(1 for r in replay if r["digest_match"] and r["outcome_match"]),
        },
        "provenance": {
            "git_rev_parse_HEAD": _sh("git rev-parse HEAD"),
            "git_status_porcelain_before_evidence": _sh("git status --porcelain"),
            "server_code_root": str(REPO_ROOT),
            "python_executable": sys.executable,
        },
    }
    out = OUT_DIR / "evaluation.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    print("saved", out)


if __name__ == "__main__":
    main()
