"""DI-1-D3 baseline runner: latency + semantic outcome + replay for the frozen corpus.

Runs backend/tests/di1_corpus/corpus.json (S1-S6, Gate-frozen 2026-10-12) against the
judged NL chain (text-plan/text-edit) on a throwaway database with its own uvicorn
subprocess. The corpus runs TWICE, each time against a fresh server process and fresh
scratch DB; per-step spec digests are compared across the two runs as the deterministic
replay evidence. All samples are reported; nothing is extrapolated.

Usage:  python reports/di1_baseline_runner.py
Output: reports/di1-latency-baseline.json
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = REPO_ROOT / "backend" / "tests" / "di1_corpus" / "corpus.json"
OUT_PATH = REPO_ROOT / "reports" / "di1-latency-baseline.json"

HEALTH_TIMEOUT_S = 60
REQUEST_TIMEOUT_S = 300


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _call(base: str, method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
            return resp.status, body, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw.decode(errors="replace")[:400]}
        return exc.code, body, (time.perf_counter() - started) * 1000


def _start_server(port: int, scratch: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    env["PID_AGENT_DATABASE_PATH"] = str(scratch / "di1-baseline.db")
    env["PID_AGENT_DIAGNOSTICS_PATH"] = str(scratch / "di1-baseline.diagnostics.jsonl")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "agentcad.main:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + HEALTH_TIMEOUT_S
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("baseline server did not become healthy")


def _entity_facts(spec: dict) -> list[dict]:
    return [
        {
            "tag": entity.get("tag", ""),
            "kind": entity.get("kind", ""),
            "symbol_key": entity.get("symbol_key", ""),
            "equipment_class": entity.get("equipment_class", ""),
            "instrument_type": entity.get("instrument_type", ""),
        }
        for entity in spec.get("entities", [])
    ]


def _connection_facts(spec: dict) -> list[dict]:
    by_id = {e.get("engineering_id"): e.get("tag", "") for e in spec.get("entities", [])}
    return [
        {
            "source": by_id.get(conn.get("source_engineering_id"), conn.get("source_engineering_id")),
            "target": by_id.get(conn.get("target_engineering_id"), conn.get("target_engineering_id")),
            "medium": conn.get("medium", ""),
        }
        for conn in spec.get("connections", [])
    ]


def _summarize(status: int, body: dict, wall_ms: float) -> dict:
    detail = body.get("detail") if isinstance(body, dict) else None
    summary: dict = {"http": status, "wall_ms": round(wall_ms, 1)}
    if isinstance(detail, dict):
        summary["outcome"] = f"{status}:{detail.get('code')}"
        summary["completeness"] = detail.get("completeness")
        summary["skipped"] = detail.get("skipped", [])
        summary["undelivered"] = detail.get("undelivered", [])
        summary["catalog_gaps"] = [
            f"{g.get('requested_type')}/{g.get('requested_tag', '')}"
            for g in detail.get("catalog_gaps", [])
        ]
        return summary
    if isinstance(detail, str):
        summary["outcome"] = f"{status}:error"
        summary["detail"] = detail[:300]
        return summary
    spec = body.get("spec", {})
    summary.update(
        outcome="committed" if body.get("committed") else "preview",
        revision=body.get("revision"),
        completeness=body.get("completeness"),
        latency_ms=body.get("latency_ms"),
        judgment_count=body.get("judgment_count"),
        spec_digest=body.get("spec_digest", "")[:16],
        entities=_entity_facts(spec),
        connections=_connection_facts(spec),
        undelivered=body.get("undelivered", []),
        skipped=body.get("skipped", []),
        unknown_tags=body.get("unknown_tags", []),
        catalog_gaps=[
            f"{g.get('requested_type')}/{g.get('requested_tag', '')}"
            for g in body.get("catalog_gaps", [])
        ],
        port_ambiguities=len(body.get("port_ambiguities", [])),
        model=body.get("model", ""),
    )
    return summary


def _check_expected(expected: dict, steps: list[dict]) -> dict:
    """Semantic correctness for the scenario: tags and tag-level connections of the final
    committed step against the corpus expectation. Observational symbol keys stay in the
    step records; the assertion level is what the user asked for (tags + connections)."""

    committed = [s for s in steps if s.get("outcome") == "committed"]
    if not committed:
        return {"tags_ok": False, "connections_ok": False, "reason": "no committed step"}
    final = committed[-1]
    tags = {e["tag"] for e in final.get("entities", [])}
    tags_missing = [t for t in expected["tags"] if t not in tags]
    pairs = {(c["source"], c["target"]) for c in final.get("connections", [])}
    connections_missing = [
        f"{c['source']}->{c['target']}"
        for c in expected["connections"]
        if (c["source"], c["target"]) not in pairs
    ]
    return {
        "tags_ok": not tags_missing,
        "tags_missing": tags_missing,
        "connections_ok": not connections_missing,
        "connections_missing": connections_missing,
        "completeness": final.get("completeness"),
        "expected_completeness": expected["completeness"],
    }


def _run_corpus_once(base: str, corpus: dict) -> dict:
    scenarios: dict[str, dict] = {}
    for scenario in corpus["scenarios"]:
        status, doc, _ = _call(base, "POST", "/documents", {"name": f"di1-{scenario['id']}"})
        assert status in (200, 201), f"create document failed: {status} {doc}"
        document_id = doc["id"]
        entry = {"document_id": document_id, "steps": []}
        last_digest, last_revision = "", 0
        for step in scenario["steps"]:
            if step["kind"] == "edit":
                payload = {
                    "sentence": step["sentence"],
                    "expected_revision": last_revision,
                    "base_spec_digest": last_digest,
                }
                endpoint = f"/documents/{document_id}/agent/text-edit"
            else:
                payload = {"sentence": step["sentence"]}
                endpoint = f"/documents/{document_id}/agent/text-plan"
            status, body, wall_ms = _call(base, "POST", endpoint, payload)
            summary = _summarize(status, body, wall_ms)
            summary["kind"] = step["kind"]
            summary["sentence"] = step["sentence"]
            entry["steps"].append(summary)
            if status == 200 and body.get("committed"):
                last_digest = body["spec_digest"]
                last_revision = body["revision"]
        entry["semantic_check"] = _check_expected(scenario["expected"], entry["steps"])
        scenarios[scenario["id"]] = entry
        print(f"  {scenario['id']}: " + json.dumps(
            [s["outcome"] for s in entry["steps"]], ensure_ascii=False
        ))
    return scenarios


def _run_gap_probes(base: str, corpus: dict) -> list[dict]:
    probes = []
    for probe in corpus["gap_probes"]:
        status, doc, _ = _call(base, "POST", "/documents", {"name": f"di1-gap-{probe['gap']}"})
        assert status in (200, 201)
        status, body, wall_ms = _call(
            base, "POST", f"/documents/{doc['id']}/agent/text-plan",
            {"sentence": probe["sentence"]},
        )
        summary = _summarize(status, body, wall_ms)
        probes.append({
            "gap": probe["gap"],
            "title": probe["title"],
            "source": probe["source"],
            "sentence": probe["sentence"],
            **summary,
        })
        print(f"  gap {probe['gap']}: {summary['outcome']}")
    return probes


def _sh(command: str) -> str:
    return subprocess.run(
        command, shell=True, capture_output=True, text=True, cwd=REPO_ROOT
    ).stdout.strip()


def main() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert [s["id"] for s in corpus["scenarios"]] == ["S1", "S2", "S3", "S4", "S5", "S6"], (
        "corpus drift: the runner refuses to measure anything but the frozen S1-S6 set"
    )

    runs = []
    for run_index in (1, 2):
        scratch = Path(tempfile.mkdtemp(prefix=f"di1-baseline-{run_index}-"))
        port = _free_port()
        proc = _start_server(port, scratch)
        base = f"http://127.0.0.1:{port}/api/v2"
        print(f"== run {run_index}: server pid={proc.pid} port={port} scratch={scratch}")
        try:
            started = time.perf_counter()
            scenarios = _run_corpus_once(base, corpus)
            probes = _run_gap_probes(base, corpus) if run_index == 1 else []
            total_ms = (time.perf_counter() - started) * 1000
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        runs.append({"run": run_index, "scenarios": scenarios, "gap_probes": probes,
                     "total_ms": round(total_ms, 1), "server_port": port})

    # Deterministic replay evidence: identical per-step spec digests across the two runs.
    replay = []
    for scenario in corpus["scenarios"]:
        digests = []
        for run in runs:
            steps = run["scenarios"][scenario["id"]]["steps"]
            digests.append([s.get("spec_digest", "") for s in steps])
        outcomes = [[s["outcome"] for s in run["scenarios"][scenario["id"]]["steps"]]
                    for run in runs]
        replay.append({
            "scenario": scenario["id"],
            "spec_digests_by_run": digests,
            "outcomes_by_run": outcomes,
            "digest_match": digests[0] == digests[1],
            "outcome_match": outcomes[0] == outcomes[1],
        })

    import platform

    result = {
        "milestone": "DI-1-D3 baseline (Gate CODE GO 2026-10-12: baseline measurement + corpus lock + gap assertion baseline only)",
        "corpus": {
            "path": "backend/tests/di1_corpus/corpus.json",
            "sha256": __import__("hashlib").sha256(CORPUS_PATH.read_bytes()).hexdigest(),
            "scenarios": [s["id"] for s in corpus["scenarios"]],
        },
        "runs": runs,
        "replay": replay,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "typesafe_model_env": os.environ.get("TYPESAFE_MODEL", "(default)"),
            "note": "local development machine, real TypeSafe endpoint, scratch DB per run; "
                    "median facts are reported per step across the two runs only -- no "
                    "extrapolation, no CI ceiling (M13 evidence-method ruling).",
        },
        "provenance": {
            "git_rev_parse_HEAD": _sh("git rev-parse HEAD"),
            "git_status_porcelain_before_evidence": _sh("git status --porcelain"),
            "server_code_root": str(REPO_ROOT),
            "python_executable": sys.executable,
        },
    }
    json.dump(result, open(OUT_PATH, "w"), ensure_ascii=False, indent=1)
    print("saved", OUT_PATH)


if __name__ == "__main__":
    main()
