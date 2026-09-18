from __future__ import annotations

import html
import os
from time import perf_counter

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from .audit import request_audit_context
from .diagnostics import DiagnosticLogger
from .llm import PlannerError
from .model_acceptance import ModelMatrixReport, ModelMatrixRequest, run_model_matrix
from .provider_security import ProviderNetworkPolicy, ProviderURLPolicyError
from .symbols import SymbolRegistry


def get_acceptance_ui() -> str:
    default_base = os.getenv("PID_AGENT_ACCEPTANCE_BASE_URL", "").strip()
    default_model = os.getenv("PID_AGENT_ACCEPTANCE_MODEL", "").strip()
    escaped_base = html.escape(default_base, quote=True)
    escaped_model = html.escape(default_model, quote=True)

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>P&ID-Agent 模型矩阵</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f8fafc;color:#0f172a}}main{{max-width:920px;margin:32px auto;padding:24px;background:white;border:1px solid #e2e8f0;border-radius:12px}}h1{{font-size:22px}}form{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}label{{display:grid;gap:5px;font-size:13px}}input{{padding:9px;border:1px solid #cbd5e1;border-radius:6px}}.wide{{grid-column:1/-1}}.check{{display:flex;align-items:center;gap:8px}}.check input{{width:16px;height:16px}}button{{padding:10px 14px;border:0;border-radius:7px;background:#2563eb;color:white;font-weight:650;cursor:pointer}}button:disabled{{opacity:.55}}pre{{max-height:520px;overflow:auto;padding:14px;background:#0f172a;color:#dbeafe;border-radius:8px;white-space:pre-wrap}}.note{{font-size:12px;color:#475569}}.actions{{display:flex;gap:10px;align-items:center}}.ok{{color:#166534}}.bad{{color:#b91c1c}}</style></head>
<body><main><h1>P&ID-Agent 真实模型验收矩阵</h1>
<p class="note">默认运行 5 个局部编辑场景：新增并连接、移动、替换、重连、连接感知删除。可额外启用 30～50 元素复杂整图场景。每个场景都检查最终拓扑；至少重复 3 次且全部通过才正式验收。API Key 仅保存在本页内存，不写入 localStorage、sessionStorage、SQLite 或诊断日志。</p>
<form id="form"><label class="wide">Base URL<input id="base" value="{escaped_base}" placeholder="https://api.openai.com/v1" required></label><label>Model<input id="model" value="{escaped_model}" placeholder="gpt-4o-mini" required></label><label>API Key<input id="key" type="password" autocomplete="off" placeholder="sk-..."></label><label>超时（秒）<input id="timeout" type="number" min="10" max="600" value="120"></label><label>重复次数<input id="repetitions" type="number" min="1" max="5" value="3"></label><label>最大重规划次数<input id="replans" type="number" min="0" max="5" value="3"></label><label class="wide check"><input id="complex" type="checkbox">增加复杂整图场景（主管、四个仪表测点、第二介质线路和标签碰撞检查）</label><div class="wide actions"><button id="run" type="submit">运行模型矩阵</button><button id="download" type="button" disabled>下载 JSON 报告</button><strong id="status"></strong></div></form>
<pre id="output">尚未运行。</pre></main><script>
let lastReport=null;const form=document.getElementById('form'),run=document.getElementById('run'),download=document.getElementById('download'),output=document.getElementById('output'),status=document.getElementById('status');
form.addEventListener('submit',async(event)=>{{event.preventDefault();run.disabled=true;download.disabled=true;status.textContent='运行中…';status.className='';output.textContent='正在调用模型并执行临时文档拓扑断言…';try{{const response=await fetch('/api/v2/acceptance/model-matrix',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{provider:{{base_url:document.getElementById('base').value.trim(),model:document.getElementById('model').value.trim(),api_key:document.getElementById('key').value||null,timeout_seconds:Number(document.getElementById('timeout').value)}},repetitions:Number(document.getElementById('repetitions').value),max_replans:Number(document.getElementById('replans').value),include_complex_diagram:document.getElementById('complex').checked}})}});const payload=await response.json();if(!response.ok)throw new Error(JSON.stringify(payload));lastReport=payload;output.textContent=JSON.stringify(payload,null,2);status.textContent=payload.accepted?'通过':'未通过';status.className=payload.accepted?'ok':'bad';download.disabled=false;}}catch(error){{status.textContent='运行失败';status.className='bad';output.textContent=String(error);}}finally{{run.disabled=false;}}}});
download.addEventListener('click',()=>{{if(!lastReport)return;const blob=new Blob([JSON.stringify(lastReport,null,2)],{{type:'application/json'}}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`pid-agent-model-matrix-${{lastReport.provider_model||'report'}}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}});
</script></body></html>"""


ACCEPTANCE_UI = get_acceptance_ui()


def create_acceptance_router(
    symbols: SymbolRegistry,
    diagnostics: DiagnosticLogger | None = None,
    provider_policy: ProviderNetworkPolicy | None = None,
    audit=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent acceptance"])

    @router.get("/acceptance/model-matrix/ui", response_class=HTMLResponse)
    def model_matrix_ui():
        return get_acceptance_ui()

    def _enforce_provider_policy(base_url: str | None) -> None:
        """Apply the same provider egress boundary as the planners.

        The acceptance matrix sends real prompts (and optionally diagram context) to a
        caller-supplied provider, so it must not be able to reach a host that the
        deployment policy denies. A denied target is recorded as audit evidence.
        """
        if provider_policy is None or not base_url:
            return
        try:
            provider_policy.normalize_and_validate(base_url)
        except ProviderURLPolicyError as exc:
            if audit is not None:
                audit.record_event(
                    "provider.egress.blocked",
                    request_audit_context(
                        "acceptance.model_matrix",
                        validation_status="invalid",
                        validation_evidence={"provider_url_error": exc.category},
                    ),
                    status="rejected",
                    error_code="provider_policy_blocked",
                    evidence={
                        "endpoint": "/api/v2/acceptance/model-matrix",
                        "provider_category": exc.category,
                    },
                )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "provider_policy_blocked",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc

    @router.post("/acceptance/model-matrix", response_model=ModelMatrixReport)
    def model_matrix(request: ModelMatrixRequest):
        started = perf_counter()
        _enforce_provider_policy(request.provider.base_url)
        if diagnostics is not None:
            diagnostics.emit(
                "acceptance.model_matrix.started",
                provider_base_url=request.provider.base_url,
                provider_model=request.provider.model,
                repetitions=request.repetitions,
                max_replans=request.max_replans,
                include_complex_diagram=request.include_complex_diagram,
                api_key_present=bool(request.provider.api_key),
            )
        try:
            report = run_model_matrix(request, symbols)
        except PlannerError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "acceptance.model_matrix.failed",
                    provider_base_url=request.provider.base_url,
                    provider_model=request.provider.model,
                    include_complex_diagram=request.include_complex_diagram,
                    error_code=exc.code,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "acceptance.model_matrix.completed",
                provider_base_url=report.provider_base_url,
                provider_model=report.provider_model,
                repetitions=report.repetitions,
                minimum_acceptance_repetitions=report.minimum_acceptance_repetitions,
                include_complex_diagram=request.include_complex_diagram,
                total_cases=report.total_cases,
                passed_cases=report.passed_cases,
                failed_cases=report.failed_cases,
                blocked_cases=report.blocked_cases,
                pass_rate=report.pass_rate,
                convergence_rate=report.convergence_rate,
                accepted=report.accepted,
                cases=[case.model_dump(mode="json") for case in report.cases],
                duration_ms=round((perf_counter() - started) * 1000, 2),
            )
        return report

    return router
