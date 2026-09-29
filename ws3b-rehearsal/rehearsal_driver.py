"""M9-WS3B shadow rehearsal driver (main@7d5cfa3 runtime, port 8002).

Executes the Gate-approved seven-step acceptance script against a disposable
backend running the exact merged main code. Every artifact is marked
REHEARSAL — synthetic HOLDOUT-1 scope, no Owner input, and therefore NOT
usable as the M9-WS3B real-pilot evidence.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

ROOT = "http://127.0.0.1:8002/api/v2"
OUT = Path(__file__).resolve().parent
RESULT: dict = {"steps": []}


def step(name: str, ok: bool, detail: dict) -> None:
    RESULT["steps"].append({"step": name, "ok": ok, "detail": detail})
    print(("PASS " if ok else "FAIL ") + name, json.dumps(detail, ensure_ascii=False)[:300])


def main() -> int:
    client = httpx.Client(base_url=ROOT, timeout=60)

    # ---- Step 1: create the pilot drawing through the human editor path ----
    created = client.post("/documents", json={"name": "WS3B 预演 · HOLDOUT-1 小流程", "width": 1600, "height": 900})
    created.raise_for_status()
    document_id = created.json()["id"]

    # Symbol placement: left-to-right horizontal chain, port-y aligned.
    # Port offsets are read from the registry to keep the chain globally y-unique.
    import subprocess

    port_query = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; sys.path.insert(0, r'/Users/joe/ai/reasonix/projects/active/P055-PID-Agent-main/backend');\n"
            "from agentcad.symbols import SymbolRegistry\n"
            "r = SymbolRegistry()\n"
            "out = {}\n"
            "for key, pid in [('buffer_tank','out'),('buffer_tank','in'),('centrifugal_pump','suction'),"
            "('centrifugal_pump','discharge'),('heat_exchanger_horizontal_shell','tube_in'),"
            "('heat_exchanger_horizontal_shell','tube_out'),('control_valve','in'),('control_valve','out')]:\n"
            "    d = r.get(key)\n"
            "    p = next(p for p in d.ports if p.id == pid)\n"
            "    out[f'{key}.{pid}'] = [p.x, p.y]\n"
            "print(json.dumps(out))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    ports = json.loads(port_query.stdout.strip().splitlines()[-1])

    def place(symbol: str, tag: str, x: float, port_ref: str, align_y: float) -> dict:
        """Place a symbol so that `port_ref` sits at vertical coordinate align_y."""
        px, py = ports[port_ref]
        return {
            "type": "symbol",
            "id": tag,
            "symbol_key": symbol,
            "position": {"x": x, "y": align_y - py},
            "width": 0,  # filled below from registry defaults
            "height": 0,
            "label": tag,
        }

    # Resolve native sizes.
    size_query = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; sys.path.insert(0, r'/Users/joe/ai/reasonix/projects/active/P055-PID-Agent-main/backend');\n"
            "from agentcad.symbols import SymbolRegistry\n"
            "r = SymbolRegistry()\n"
            "print(json.dumps({k: [r.get(k).width, r.get(k).height] for k in "
            "['buffer_tank','centrifugal_pump','heat_exchanger_horizontal_shell','control_valve']}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    sizes = json.loads(size_query.stdout.strip().splitlines()[-1])

    CHAIN_Y = 400.0
    layout = [
        ("buffer_tank", "V-101", 120.0, "buffer_tank.out"),
        ("centrifugal_pump", "P-101", 330.0, "centrifugal_pump.suction"),
        ("heat_exchanger_horizontal_shell", "E-101", 560.0, "heat_exchanger_horizontal_shell.tube_in"),
        ("control_valve", "FCV-101", 830.0, "control_valve.in"),
        ("buffer_tank", "V-102", 1060.0, "buffer_tank.in"),
    ]
    elements = []
    positions: dict[str, tuple[float, float]] = {}
    for symbol, tag, x, port_ref in layout:
        element = place(symbol, tag, x, port_ref, CHAIN_Y)
        element["width"], element["height"] = sizes[symbol]
        elements.append({"op": "add_element", "element": element})
        px, py = ports[port_ref]
        positions[tag] = (x, CHAIN_Y - py)

    def abs_port(tag: str, port_key: str) -> dict:
        x, y = positions[tag]
        px, py = ports[port_key]
        return {"x": x + px, "y": y + py}

    chain = [
        ("V-101", "buffer_tank.out", "P-101", "centrifugal_pump.suction"),
        ("P-101", "centrifugal_pump.discharge", "E-101", "heat_exchanger_horizontal_shell.tube_in"),
        ("E-101", "heat_exchanger_horizontal_shell.tube_out", "FCV-101", "control_valve.in"),
        ("FCV-101", "control_valve.out", "V-102", "buffer_tank.in"),
    ]
    for index, (src_tag, src_port, tgt_tag, tgt_port) in enumerate(chain, start=1):
        source = abs_port(src_tag, src_port)
        target = abs_port(tgt_tag, tgt_port)
        elements.append(
            {
                "op": "add_element",
                "element": {
                    "type": "connector",
                    "id": f"C-{index:03d}",
                    "points": [source, target],
                    "source": {"element_id": src_tag, "port_id": src_port.split(".")[-1], "point": source},
                    "target": {"element_id": tgt_tag, "port_id": tgt_port.split(".")[-1], "point": target},
                    "routing": "orthogonal",
                    "process_tag": f"PL-{index:03d}",
                    "medium": "process",
                    "nominal_diameter": "DN50",
                    "flow_direction": "forward",
                    "arrow_position": "end",
                },
            }
        )

    tx = client.post(
        f"/documents/{document_id}/transactions",
        json={"expected_revision": 0, "label": "预演建图：HOLDOUT-1 句一（人工编辑通道）", "operations": elements},
    )
    step("1.建图（人工 transactions 通道）", tx.status_code == 200, {"status": tx.status_code, "body": tx.text[:400]})
    if tx.status_code != 200:
        return 1
    document = client.get(f"/documents/{document_id}").json()
    step("1b.图纸状态", document["revision"] == 1 and len(document["elements"]) == 9,
         {"revision": document["revision"], "elements": len(document["elements"])})

    # ---- Step 2: fresh release readiness (also re-run inside approval/release) ----
    readiness = client.get(f"/validation/documents/{document_id}/release-readiness")
    step("2.release-readiness 可读", readiness.status_code == 200,
         {"status": readiness.status_code, "body": readiness.text[:300]})

    # ---- operator session ----
    session = client.post("/review/operator-session")
    session.raise_for_status()
    token = session.json()["operator_token"]
    headers = {"X-Operator-Token": token}

    # ---- Step 3: review loop (simulated comments, marked REHEARSAL) ----
    view = client.get(f"/documents/{document_id}/review").json()
    seq = view["governance_seq"]
    thread = client.post(
        f"/documents/{document_id}/review/threads",
        json={"body": "【预演·模拟意见】泵出口建议补流向箭头与管径标注", "expected_governance_seq": seq},
        headers=headers,
    )
    step("3.审查线程（模拟）", thread.status_code == 200, {"status": thread.status_code})
    thread_id = thread.json()["threads"][0]["thread_id"]
    seq = thread.json()["governance_seq"]
    resolved = client.post(
        f"/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "【预演】已补流向箭头与 DN50 标注", "expected_governance_seq": seq},
        headers=headers,
    )
    step("3b.审查闭环（模拟）", resolved.status_code == 200, {"status": resolved.status_code})
    seq = resolved.json()["governance_seq"]

    # ---- Step 4: approval request (agent) + operator decision ----
    requested = client.post(
        f"/documents/{document_id}/approval/request", json={"expected_governance_seq": seq}
    )
    step("4.申请工程批准", requested.status_code == 200, {"status": requested.status_code})
    approval_id = requested.json()["approvals"][-1]["approval_id"]
    seq = requested.json()["governance_seq"]
    decided = client.post(
        f"/documents/{document_id}/approval/{approval_id}/decide",
        json={"decision": "approved", "expected_governance_seq": seq},
        headers=headers,
    )
    step("4b.批准决策", decided.status_code == 200, {"status": decided.status_code, "body": decided.text[:300]})
    if decided.status_code != 200:
        return 1
    seq = decided.json()["governance_seq"]

    # ---- Step 5: release + evidence package ----
    released = client.post(
        f"/documents/{document_id}/releases", json={"expected_governance_seq": seq}, headers=headers
    )
    step("5.正式发布", released.status_code == 200, {"status": released.status_code, "body": released.text[:400]})
    if released.status_code != 200:
        return 1
    release = released.json()["releases"][-1]
    package = client.get(f"/documents/{document_id}/releases/{release['release_id']}/evidence.zip")
    step("5b.证据包下载", package.status_code == 200, {"status": package.status_code, "bytes": len(package.content)})
    if package.status_code != 200:
        return 1
    (OUT / "rehearsal-evidence.zip").write_bytes(package.content)
    digest = hashlib.sha256(package.content).hexdigest()
    step("5c.包哈希回核", digest == release["package_sha256"],
         {"sha256": digest, "record": release["package_sha256"]})

    with zipfile.ZipFile(BytesIO(package.content)) as archive:
        names = sorted(archive.namelist())
        manifest = archive.read("MANIFEST.sha256").decode("utf-8")
    step("5d.MANIFEST 成员", names == sorted(["MANIFEST.sha256", "approval.json", "audit-chain.json", "drawing.dxf", "drawing.pdf", "release-readiness.json", "release.json", "validation-report.json"]),
         {"members": names})
    (OUT / "rehearsal-manifest.txt").write_text(manifest, encoding="utf-8")

    RESULT["document_id"] = document_id
    RESULT["release"] = release
    RESULT["package_sha256"] = digest
    RESULT["verdict"] = "REHEARSAL ONLY — synthetic HOLDOUT-1, simulated review comments, no Owner input; NOT M9-WS3B real-pilot evidence."
    (OUT / "rehearsal-result.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print("verdict:", RESULT["verdict"])
    return 0 if all(s["ok"] for s in RESULT["steps"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
