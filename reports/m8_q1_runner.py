"""M8-Q1 read-only runner: 8 scenarios against the signed chain, scratch DB only.

Writes happen ONLY inside data/m8-q1-scratch.db, a throwaway file deleted after the
run; the product system, catalogue, digests and main are untouched. Second-turn edit
sentences mechanically require a stored source, so scratch commits stand in for a real
r1 -- declared, not hidden.
"""
import json
import sys
import urllib.request
from pathlib import Path

# Repo-relative import (P0-RUNTIME-PROVENANCE): resolve the backend from this file's
# location, never from an absolute path that can silently bind to a parallel worktree.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
from agentcad.cad_dxf import read_dxf

BASE = "http://127.0.0.1:8002/api/v2"

def call(method, path, payload=None, raw=False):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, (body if raw else json.loads(body))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body.decode(errors="replace")[:300]}

def summarize(result):
    if isinstance(result, dict) and "detail" in result and isinstance(result["detail"], dict):
        d = result["detail"]
        return {
            "outcome": f"422:{d.get('code')}",
            "completeness": d.get("completeness"),
            "undelivered": d.get("undelivered", [])[:4],
            "catalog_gaps": [g.get("requested_type") + "/" + g.get("requested_tag", "") for g in d.get("catalog_gaps", [])],
            "port_ambiguities": [a.get("reason") + ":" + a.get("symbol_key", "") for a in d.get("port_ambiguities", [])],
        }
    return {
        "outcome": "committed" if result.get("committed") else "preview",
        "revision": result.get("revision"),
        "completeness": result.get("completeness"),
        "entities": [e["tag"] for e in result.get("spec", {}).get("entities", [])],
        "connections": len(result.get("spec", {}).get("connections", [])),
        "spec_digest": result.get("spec_digest", "")[:16],
        "port_ambiguities": len(result.get("port_ambiguities", [])),
    }

SCENARIOS = [
    ("DEV-1", [
        ("plan", "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"),
        ("edit", "再添加一个缓冲罐 V-102，把 P-101 的出口接到 V-102 的入口"),
    ]),
    ("DEV-2", [
        ("expect-receipt", "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一个管壳式换热器 E-101，把 V-101 接到 P-101，把 P-101 接到 E-101"),
        ("plan", "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一个管壳式换热器 E-101，把 V-101 接到 P-101，把 P-101 接到 E-101 的管程入口"),
    ]),
    ("DEV-3", [
        ("plan", "添加一台离心泵 P-101，添加一个闸阀 XV-101，添加一个控制阀 FCV-101，添加一个缓冲罐 V-101，把 P-101 接到 XV-101，把 XV-101 接到 FCV-101，把 FCV-101 接到 V-101"),
        ("edit", "再添加一个安全阀 PSV-101，把 P-101 的出口接到 PSV-101 的入口，把 PSV-101 的出口接到 V-101 的顶部管口"),
    ]),
    ("DEV-4", [
        ("expect-receipt", "添加一个缓冲罐 V-101，给 V-101 添加一台液位计 LIT-101、一台压力表 PIT-101 和一台温度变送器 TT-101"),
        ("edit", "添加一台离心泵 P-101，把 V-101 接到 P-101，在 P-101 的出口加一台流量计 FIT-101"),
    ]),
    ("DEV-5", [
        ("expect-receipt", "添加一个主工艺系统，添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一个管壳式换热器 E-101，把 V-101 接到 P-101，把 P-101 接到 E-101 的管程入口"),
    ]),
    ("DEV-6", [
        ("plan", "添加一台进料泵 P-101，添加一个精馏塔 T-101，把 P-101 接到 T-101 的原料进料口"),
        ("edit", "添加一个冷凝器 E-101，把 T-101 的塔顶气相出口接到 E-101 的工艺入口，把 E-101 的工艺出口接到 T-101 的回流入口"),
        ("edit", "把 T-101 的侧线采出口接到一个缓冲罐 V-102"),
    ]),
    ("HOLDOUT-1", [
        ("plan", "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一个管壳式换热器 E-101，添加一个控制阀 FCV-101，添加一个缓冲罐 V-102，把 V-101 接到 P-101，把 P-101 接到 E-101 的管程入口，把 E-101 的管程出口接到 FCV-101，把 FCV-101 接到 V-102"),
        ("edit", "把 FCV-101 换成一个闸阀"),
    ]),
    ("HOLDOUT-2", [
        ("plan", "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一台离心泵 P-102，添加一个单向阀 CV-101，添加一个缓冲罐 V-102，把 V-101 接到 P-101，把 V-101 接到 P-102，把 P-101 接到 CV-101，把 P-102 接到 CV-101，把 CV-101 接到 V-102"),
        ("edit", "删除 P-102"),
    ]),
]

results = {}
for scenario_id, steps in SCENARIOS:
    _, doc = call("POST", "/documents", {"name": f"m8-{scenario_id}"})
    document_id = doc["id"]
    entry = {"document_id": document_id, "steps": []}
    last_digest, last_revision = "", 0
    for kind, sentence in steps:
        if kind in ("plan", "expect-receipt"):
            status, body = call("POST", f"/documents/{document_id}/agent/text-plan",
                                {"sentence": sentence})
        else:
            status, body = call("POST", f"/documents/{document_id}/agent/text-edit",
                                {"sentence": sentence, "expected_revision": last_revision,
                                 "base_spec_digest": last_digest})
        summary = summarize(body) if status != 200 or isinstance(body, dict) else {"raw": str(body)[:200]}
        summary["http"] = status
        entry["steps"].append({"kind": kind, "sentence": sentence, **summary})
        if status == 200 and body.get("committed"):
            last_digest = body["spec_digest"]
            last_revision = body["revision"]
    # export readback on the final state
    exports = {}
    for fmt in ("pdf", "dxf"):
        code, data = call("GET", f"/documents/{document_id}/export-v2.{fmt}?range=content&padding=24", raw=True)
        if code == 200 and fmt == "dxf":
            parsed = read_dxf(data)
            kinds = {}
            for prim in parsed.primitives:
                k = getattr(prim, "kind", type(prim).__name__)
                kinds[k] = kinds.get(k, 0) + 1
            exports[fmt] = {"http": code, "bytes": len(data), "kinds": kinds}
        else:
            exports[fmt] = {"http": code, "bytes": len(data) if isinstance(data, bytes) else 0}
    entry["exports"] = exports
    results[scenario_id] = entry
    print(f"== {scenario_id}: " + json.dumps([s["outcome"] for s in entry["steps"]], ensure_ascii=False))

out = str(REPO_ROOT / "reports" / "m8-q1-raw.json")
json.dump(results, open(out, "w"), ensure_ascii=False, indent=1)
print("saved", out)
