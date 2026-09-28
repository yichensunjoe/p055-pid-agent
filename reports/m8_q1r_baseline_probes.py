"""M8-Q1R targeted baseline probes on exact main @ 1afeccab (Gate-ordered, pre-code).

Three probes:
  1. DEV-2 selector chain: receipt -> restate 管程入口 -> expect selected/complete (G1 repro)
  2. DEV-1 entity DXF: plan -> export-v2.dxf -> read_dxf non-empty (G5 repro)
  3. HOLDOUT-2 atomicity: 409 -> GET state must equal pre-request state (P1 repro, expected FAIL)

Provenance block recorded at top of the evidence JSON.
"""
import json, subprocess, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:8002/api/v2"
WT = "/Users/joe/ai/reasonix/projects/active/P055-PID-Agent-q1r"
MAIN = "/Users/joe/ai/reasonix/projects/active/P055-PID-Agent"

sys.path.insert(0, WT + "/backend")
import agentcad  # noqa: E402
from agentcad.cad_dxf import read_dxf  # noqa: E402

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=WT).stdout.strip()

provenance = {
    "git_rev_parse_HEAD": sh("git rev-parse HEAD"),
    "git_status_porcelain": sh("git status --porcelain"),
    "server_cwd": WT,
    "agentcad___file__": agentcad.__file__,
    "python_executable": sys.executable,
}

def call(method, path, payload=None, raw=False):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read()
            return resp.status, (body if raw else json.loads(body))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body.decode(errors="replace")[:300]}

def snap(doc_id):
    s, b = call("GET", f"/documents/{doc_id}")
    return {"http": s, "revision": b.get("revision"),
            "n_elements": len(b.get("elements", [])),
            "systems": [sy["name"] for sy in b.get("systems", [])]}

def detail_code(b):
    d = b.get("detail") if isinstance(b, dict) else None
    return d.get("code") if isinstance(d, dict) else None

results = {"provenance": provenance, "probes": {}}

# ---- Probe 1: DEV-2 selector chain (G1) ----
_, doc = call("POST", "/documents", {"name": "q1r-dev2"})
D = doc["id"]
p1 = {"document_id": D}
s1, b1 = call("POST", f"/documents/{D}/agent/text-plan",
              {"sentence": "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一个管壳式换热器 E-101，把 V-101 接到 P-101，把 P-101 接到 E-101"})
p1["step1_receipt"] = {"http": s1, "code": detail_code(b1),
                       "ambiguities": [a.get("reason") for a in (b1.get("detail") or {}).get("port_ambiguities", [])]}
s2, b2 = call("POST", f"/documents/{D}/agent/text-plan",
              {"sentence": "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一个管壳式换热器 E-101，把 V-101 接到 P-101，把 P-101 接到 E-101 的管程入口"})
p1["step2_restate"] = {"http": s2, "outcome": b2.get("outcome") or detail_code(b2),
                       "completeness": b2.get("completeness"), "revision": b2.get("revision"),
                       "spec_digest": (b2.get("spec_digest") or "")[:16]}
results["probes"]["g1_dev2_selector"] = p1

# ---- Probe 2: DEV-1 entity DXF (G5) ----
_, doc = call("POST", "/documents", {"name": "q1r-dev1"})
D = doc["id"]
p2 = {"document_id": D}
s, b = call("POST", f"/documents/{D}/agent/text-plan",
            {"sentence": "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"})
p2["plan"] = {"http": s, "completeness": b.get("completeness"), "revision": b.get("revision")}
code, data = call("GET", f"/documents/{D}/export-v2.dxf?range=content&padding=24", raw=True)
if code == 200:
    parsed = read_dxf(data)
    kinds = {}
    for prim in parsed.primitives:
        k = getattr(prim, "kind", type(prim).__name__)
        kinds[k] = kinds.get(k, 0) + 1
    p2["dxf"] = {"http": code, "bytes": len(data), "kinds": kinds}
else:
    p2["dxf"] = {"http": code, "error": data.get("detail") if isinstance(data, dict) else str(data)[:200]}
results["probes"]["g5_dev1_dxf"] = p2

# ---- Probe 3: HOLDOUT-2 atomicity (P1, expect FAIL = partial commit) ----
_, doc = call("POST", "/documents", {"name": "q1r-h2"})
D = doc["id"]
before = snap(D)
s, b = call("POST", f"/documents/{D}/agent/text-plan",
            {"sentence": "添加一个缓冲罐 V-101，添加一台离心泵 P-101，添加一台离心泵 P-102，添加一个单向阀 CV-101，添加一个缓冲罐 V-102，把 V-101 接到 P-101，把 V-101 接到 P-102，把 P-101 接到 CV-101，把 P-102 接到 CV-101，把 CV-101 接到 V-102"})
after = snap(D)
results["probes"]["p1_holdout2_atomicity"] = {
    "document_id": D, "http": s,
    "detail": b.get("detail") if isinstance(b, dict) else str(b)[:200],
    "before": before, "after": after,
    "state_invariant": before == after,
}

# ---- Isolation guard (P2 retained test, control-style) ----
_, doc = call("POST", "/documents", {"name": "q1r-iso-B"})
B = doc["id"]
s, b = call("POST", f"/documents/{B}/agent/text-plan",
            {"sentence": "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"})
results["probes"]["p2_iso_guard"] = {"document_id": B, "http": s, "after": snap(B)}

out = MAIN + "/reports/m8-q1r-baseline-raw.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)
print("saved", out)
print(json.dumps(results["probes"], ensure_ascii=False, indent=1)[:2000])
