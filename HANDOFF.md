# HANDOFF — P055-PID-Agent

> 交接文档：每次开新会话先读本文件。更新规则见 `AGENTS.md`「HANDOFF 交接规则」。

## 当前状态（2026-09-18，最新轮次：T0.5 + Registry 收口 + M2 工程语义图）

- **T0.5 Audit / Provenance 已完成**：SQLite schema v4 新增 append-only `audit_records`（sha256 哈希链，genesis → prev_hash/record_hash/ordinal），`audit.py` 负责把 revision 写入、semantic diff、validation evidence、approval/intent 绑定在同一条证据里；**所有写路径**（v2 文档/事务/undo/redo/重命名/移动、imports、project settings、MCP 全部 apply、legacy v1 primitive/layer、Agent harness allow/ask/deny、被拒绝的审批）都在**同一 SQLite 事务**内写入审计记录，不存在未归因的 revision。
- **T0.5 新增只读证据面**：REST `GET /api/v2/audit/records|verify|export`、`GET /api/v2/documents/{id}/audit`、`GET /api/v2/documents/{id}/history/{revision}/evidence`；MCP `get_audit_trail` / `verify_audit_chain` / `get_revision_evidence` / `get_agent_session_audit`；CLI `pid-agent audit verify|trail|export|evidence`。文档见 `docs/audit-and-provenance.md`。
- **Registry 收口（T0.3 补全）**：新增 `surface_contract.py`，把每个 HTTP 路由与 MCP tool 声明为 read / engineering_write / legacy_write / harness_lifecycle / runtime | agent_runtime / model_acceptance / project_metadata，并标记是否必须审计；`tests/test_surface_contract.py` 用**实时 OpenAPI + 实时 MCP tool 列表**反向校验，任何新增未登记的写路径、未审计的工程写、未绑定的 side-effect tool 都会直接测试失败。
- **M2 工程语义图已完成**：新增 `engineering_ir.py`（equipment/valve/instrument/line/junction/off_page_connector/annotation/graphic 工程对象、稳定工程标识、管线聚合、信号分类、OPC 跨图、拓扑边与连通分量、`IR_*` findings、flow-aware trace）与 `project_index.py`（SQLite schema v5 `project_index` 派生索引：revision + content_hash + graph_hash + builder_version，cheap/verified 两级新鲜度、显式失效原因、自动修复、跨图 OPC 解析）。REST/MCP/CLI 三面齐备，离线 quality harness 新增 `engineering_graph_contract`。文档见 `docs/engineering-semantic-graph.md`。
- **M2 前端**：右侧新增只读「工程图谱」tab（`frontend/src/editor/EngineeringGraphPanel.tsx` + 纯函数层 `frontend/src/engineeringGraph.ts`），展示工程对象分组、findings 严重度筛选、对象/管线定位、flow-aware trace 步骤与遍历管线、项目索引新鲜度/总计/跨图链接与显式重建按钮；新增 `frontend/tests/engineeringGraph.test.ts`（7 例）与 `frontend/e2e/engineering-graph.spec.ts`。
- **M2 明确边界**：IR 是 `(document, symbol registry)` 的纯函数，永不写文档；索引是派生缓存、不进审计链（但显式 rebuild 会记 `engineering.index.rebuilt`）；不建模 signal loop / ISA 位号结构 / SIS 联锁；未打标签对象只有 element 级标识（`identity_scope` 如实标注）；trace 目前是文本步骤列表，尚无拓扑图可视化；Agent 上下文仍走 `build_agent_harness_context`，与本图层尚未统一（属 M3/M4）。
- 已完成 Charter Priority 0 / T0.1–T0.4 首轮：Canonical Tool Registry、Agent Session、Permission/Approval Gate、Semantic Engineering Diff。
- Tool Registry 已统一 schema/permission/risk/side-effect/preview/idempotency/audit/surface；Agent/MCP/REST 共用能力定义。
- SQLite schema v3 已持久化 `agent_sessions`、`agent_approvals`、`agent_tool_calls`；approval 严格绑定 session + tool + document + canonical intent hash，成功执行后 consumed，旧 MCP/REST Agent mutation 旁路已封堵。
- semantic `plan-v2/stream` 创建 session，replan 续用，apply-v2 需要 session_id + approval_id；网页手动和自动 Agent 都在工程写入前停在人工确认点。
- T0.4 新增 `semantic_diff.py` / `semantic_diff_models.py`：将 raw before/after snapshot 转成 equipment/valve/instrument/pipeline 等工程对象级差异，输出 field delta、change type、human-readable summary 与 risk_hint。
- REST 新增无写入 `POST /documents/{id}/transactions/semantic-diff` 与按 revision 查询 `GET /documents/{id}/history/{revision}/semantic-diff`；MCP 新增 `preview_semantic_diff`。新 revision 的 history details 会持久保存 Semantic Diff。
- Semantic Diff 的 `risk_hint` 仅为 deterministic review hint，不替代未来 Rule Engine / safety rules 的正式工程风险判断。
- 最新核心验证（T0.5 + M2 代码）：Ruff **All checks passed**；offline quality harness **5/5 passed**；backend pytest **359 passed**；frontend `npm test` **105 passed**、`npm run build` ✓；schema **v5**（v3→v5 / v4→v5 迁移测试已覆盖，另有 v4 副本→v5 迁移→审计链仍 valid→索引 rebuild 35/35→backup/restore schema 5 的完整演练）；真实项目库（35 图）`rebuild_all` **35/35、~300 ms、0 stale**、跨图 OPC 解析 1 条、`audit verify` ok。**Playwright e2e 本轮未执行**：`playwright.config.ts` 的 webServer 固定占用 8000/4173 且 `reuseExistingServer:false`，而 8000 已被另一工作区的常驻后端占用；更关键的是 e2e 的 `resetDocuments()` 会清空所连数据库中的全部图纸（共享本机 dev DB），因此不允许在当前环境对活库运行。
- Browser Playwright 仍以仓库既有 local visual baseline 问题为主要待核项；此前确认 8 个 screenshot baseline 漂移 + 1 个旧“重命名”按钮定位问题，不将其误报为 Harness 后端失败。最新 browser run 继续验证中/后续按实际结果更新。
- 下一步（**未开始，等待验收**）：M3 Validator Framework —— 把当前分散在 `diagram_quality.py` / `engineering_reports.py` / `engineering_ir.py` 的 deterministic 规则收拢为可注册、可版本化、可按项目标准加载的 validator 框架（每条规则带 id/severity/scope/evidence/项目标准引用），并让 IR findings 成为其消费者；随后再进 M4 Deterministic Drafting / Routing 与 Agent Repair Loop。

## 近期轮次（最新在上，保留全部）

- 2026-09-18（T0.5 Audit/Provenance + Registry 收口 + M2 工程语义图，本轮，未提交待验收）：
  - **证据链**：schema v4 `audit_records`（`record_id/ordinal/recorded_at/event_type/actor/surface/tool_name/status/document_id/base_revision/result_revision/session_id/approval_id/tool_call_id/provider/model/label/intent_hash/diff_hash/diff_preview_hash/diff_binding/validation_status/validation_hash/evidence_json/error_code/prev_hash/record_hash`）；`audit_hash.py` 单点计算链哈希；`store.save()` 与 `store.record_audit_event()` 把文档行、history（含 semantic diff）、审计行、tool call、approval 消费、session 关单**放在同一个 `BEGIN IMMEDIATE` 事务**；新增 `request_context.py` 提供 request-id 关联（中间件绑定，API 只从服务端上下文取 actor/surface，绝不信任请求体）。
  - **写路径全覆盖**：v2 transactions/undo/redo/rename/folder、imports（document/project-package）、project settings、create/delete、MCP `apply_transaction`/`apply_transaction_v2`/`apply_agent_transaction`/`apply_auto_layout`、legacy v1 draw/layer 与 undo/redo、以及 **被拒绝/意图不匹配的审批**（`permission.rejected`、`validation.rejected`）都落审计；MCP 旧的 bespoke apply 路径已改为复用唯一受治理写通道（`_apply_with_history` → 同一 harness gate）。
  - **Registry 收口**：`surface_contract.py` + `tests/test_surface_contract.py` 建立“无未登记写路径”机器契约；`tool_registry.py` 为每个 side-effect tool 补 `audit_event` / `idempotency`，并新增 engineering graph / trace / project graph / index rebuild 四个能力定义（rebuild 为 `allow` + `idempotent` + 审计 `engineering.index.rebuilt`）。
  - **M2 工程语义图**：`engineering_ir.py`（稳定工程标识：tag 级优先、重复 tag 确定性 `#2` 且报错、未打标签如实降级为 element 级；管线按 tag+介质+口径聚合；信号仅按显式介质或 instrument↔instrument 分类，绝不把工艺引压点当信号；OPC 方向/目标图；拓扑边 + 连通分量 + 7 类 `IR_*` findings + flow-aware `trace_engineering_object`）；`project_index.py`（schema v5 `project_index` 缓存 + `document_content_hash` 失效检测 + `rebuild_all/prune` + 跨图 OPC 解析 + `ProjectEngineeringGraph`）。
  - **接口**：REST `GET /api/v2/documents/{id}/engineering-graph`、`.../engineering-graph/trace`、`GET /api/v2/project/engineering-graph`、`GET /api/v2/documents/{id}/project-index`、`POST /api/v2/project/index/rebuild`；MCP `get_engineering_graph` / `trace_engineering_object` / `get_project_engineering_graph` / `rebuild_project_index`；CLI `pid-agent engineering-graph`、`pid-agent project-index rebuild|list|project`（有 error finding 或 stale 时退出码 2，可直接进 CI）。
  - **测试**：新增 `tests/test_audit_provenance.py`、`tests/test_surface_contract.py`、`tests/test_engineering_ir.py`（20）、`tests/test_project_index.py`（11）、`tests/test_engineering_graph_api.py`（5），质量门新增 `engineering_graph_contract`，迁移测试覆盖 v3→v5 与 v4→v5；并修复一处**测试自身的隐性缺陷**（v3 迁移测试原先硬编码 `CURRENT_SCHEMA_VERSION - 1`，版本号一涨就失真，现改为显式 `PRAGMA user_version=3`）。
  - **真实数据交叉验证**：对仓库 30 张真实图纸 `rebuild_all`：1440 工程对象、536 拓扑边、28 个 OPC、264 ms、0 stale；同时暴露真实数据问题 18 处 `IR_SYMBOL_DEFINITION_MISSING`（如 `pressure_transmitter` 并非图例库 key）、27 处 OPC 缺 `target_document_id`、55 处孤立设备 —— 均为**既有图纸数据缺陷**，非本轮引入，作为 M3/M4 的输入保留。
  - CI 口径：Ruff✓、quality harness 5/5✓、pytest 359✓；前端本轮未改动。Playwright e2e 未复跑（8000 端口被常驻后端占用），沿用既有基线问题清单。
- 2026-09-18（T0.4 Semantic Engineering Diff）：新增工程语义 diff 模型/引擎，支持 valve/equipment/instrument/pipeline/junction/annotation 等实体分类、field delta、layout/reroute/engineering-property change 分类与 risk hint；提供 transaction 无写入 preview、revision 历史持久化和 REST/MCP 查询；新增 4 组 diff 测试。核心 CI：Ruff✓、quality harness 4/4✓、pytest 286✓、frontend unit/build✓。下一步 T0.5 Approval + Diff + Validation Evidence provenance。

- 2026-09-18（T0.2/T0.3 Agent Session + Permission/Approval Gate）：数据库升级 schema v3，新增 sessions/approvals/tool_calls；实现 exact-intent approval hash、allow/ask/deny enforcement、one-time consume、session audit；semantic plan/replan/apply、网页手动/自动 Agent、MCP semantic/low-level apply 全部接入 Harness，封堵旧低层绕过；新增 REST/MCP 管理接口、前端显式批准、集成测试与设计文档。下一步 T0.4 Semantic Diff。

- 2026-09-18（T0.1 Canonical Tool Registry）：新增统一 Tool Registry，登记 12 个现有 Harness 能力并固化 schema/permission/risk/side-effect/preview/idempotency/audit metadata；REST 新增 `/api/v2/agent/tools`，MCP 新增 `get_tool_registry`，semantic tool schema 改由 registry 派生；新增单测与设计文档。保持 DocumentService/Transaction 原子边界不变。下一步 T0.2 Session + T0.3 Permission/Approval enforcement。

- 2026-09-18（总体技术任务书与 AgentCAD Harness 长期主线）：新增 `PROJECT_CHARTER.md` v1.0.0（约 2.45 万字符），把最终“经工程校核批准后可进入施工阶段交付”的目标固化为 canonical charter；明确 semantic IR、受控 tools、validator、permission/approval、audit/provenance、project graph、release gates、benchmark 和 M0-M10；同步更新 AGENTS.md 强制未来模型先读 Charter，并在 README 建立入口。下一步优先 T0.1 Tool Registry → T0.2 Session → T0.3 Permission/Approval → T0.4 Semantic Diff → T0.5 Audit。本轮仅文档治理，无业务代码变更、未重跑测试。

- 2026-08-21（全项目代码 Review：质量声明核验 + 安全/性能深审）：实测核验 pytest 271✓ / npm test 98✓ / build✓；**ruff 实测 9 错**（security.py F821×8 缺 `Any` 导入 + F841×1），与「ruff 0 报错」声明不符；e2e 因 8000 端口被常驻后端占用未复跑。关键发现：🔴 security.py `_handle_asgi` 仅校验 Content-Length 头，chunked 请求可绕过 body 大小上限（F841 未使用变量即烂尾证据）；🔴 验收矩阵链路（api_acceptance→model_acceptance）未注入 ProviderNetworkPolicy，shared 模式下该端点可作 SSRF 跳板（llm.py 默认 local 策略放行全部地址）；🟡 长 SSE 流全程占用并发信号量槽（默认32）；🟡 前端 EditorCanvas pointermove 触发整画布重渲染、api.ts SSE `.trim()` 吞流式空白、main.tsx 挂载 4 个 MutationObserver 旁路补丁组件、store/SSE 解析零单测覆盖。下一步：① 补 `from typing import Any` 清零 ruff；② ASGI 路径落地真实 body 限制；③ create_acceptance_router 注入 provider_policy；④ 按 Top5 清单治理前端渲染与旁路组件。
- 2026-08-21（左侧侧边栏滚动冲突根除、全域滚轮穿透与导航标签体系）：移除了 .document-tree-list 内部嵌套限高，外层 .sidebar.documents-panel 统一滚动（scrollbar-gutter stable / 高对比度滑块）；左侧新增 全部/图纸/图例 三档导航与 一键折叠全部 快捷按钮；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（左侧图纸列表滚轮滚动条与左下角单位图例基础图元过滤）：优化 .document-tree-list 样式与专属滚动条（max-height 480px / 细滚动条 / overscroll-behavior contain），滚轮顺畅触达所有图纸；SymbolPalette 过滤基础图元，保持左下角单位图例纯净聚焦工艺设备；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（大模型流式传输阻塞根因修复与 Ollama / MiniMax M3 Cloud 真实联调验收）：排查并消除前置同步 post 与 Starlette BaseHTTPMiddleware TaskGroup 协程中断冲突，将 RequestDiagnostics 与 RequestBoundary 重构为原生纯 ASGI 中间件；使用本地 Ollama 与 MiniMax M3 Cloud 真实联调端到端 P&ID 出图流式测试，100% 成功生成储罐与离心泵标准对齐管线，质量评分 100.0 分；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（基础图元顶部分类快捷栏与大模型思考/生成双流式传输）：基础图元放置免属性弹窗直接落图；顶部工具栏新增 BasicShapesToolbar（几何/设备/标注/附件 4 分类，支持直接拖拽至画布）；后端实现 plan-v2-stream SSE 流式接口提取 reasoning_content 与 <think> 思考链；前端 AgentStreamingViewer 实时动态渲染思维链；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（项目分类文件夹树状归档与通用基础图元库移植）：左侧新增项目分类文件夹管理（新建/重命名/删除/折叠展开/下拉移动/搜索清空），支持新建图纸时直接指定分类；扩充 11 种通用基础图元（变更云线/六角框/八角安全框/菱形判定/圆柱体/机柜撬块/梯形槽/平行四边形/标注气泡/粗箭头/8字盲板/阻火器等）；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 41 passed 100% 全绿。
- 2026-08-21（第三步落地：属性面板三段式与图例悬停放大预览）：借鉴 draw.io 重构右侧属性面板为三段式分类（全部/工程/样式/排列），集成快捷对齐、等间距分布与编组锁；左侧图例库新增悬停放大 Popover（大号矢量预览、Badge、尺寸与端口引脚清单）与搜索一键清空；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（第一步与第二步落地：标准图元扩充与连线对齐增强）：在 standard_symbols.json 扩充 10 种工业高频图元（角阀/三通调节阀/减压阀/精馏塔/螺旋板换热器/螺杆泵/罗茨风机等）；实现智能磁吸等间距吸附（dist(A,B)==dist(B,C)）与放置吸附；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（物项专属工程属性弹窗与下拉自填体系）：实现物项放置时的分类专属工程属性弹窗（5 大类 Schema：阀门/设备/管线/仪表/管件）与右侧属性栏同步；预设丰富标准通径、压力、材质、故障位置等参数，原生支持下拉选择与自由输入自填，支持直接跳过；全量回归：pytest 271 passed，npm test 96 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（物项专属工程属性弹窗与下拉自填体系）：实现物项放置时的分类专属工程属性弹窗（5 大类 Schema：阀门/设备/管线/仪表/管件）与右侧属性栏同步；预设丰富标准通径、压力、材质、故障位置等参数，原生支持下拉选择与自由输入自填，支持直接跳过；全量回归：pytest 271 passed，npm test 96 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（取消超时硬顶与随时手动叫停机制）：针对本地大模型生成耗时较长的特点，取消超时硬编码与 600s 校验上限（默认无超时持续等待）；移除前端超时数字输入限制；全链路接入 AbortController，手动模式与自动完成均新增即时「停止 / 叫停」按钮；全量回归：pytest 271 passed，ruff 0，npm test 92 passed，e2e 39 passed 100% 全绿。
- 2026-08-21（厂商解耦与零默认模型纯净化）：彻底移除源码与预设中任何厂商名称（如 Kimi）与默认模型名（`defaultModel` 全部置空）；`providerPresets.ts` 纯净化为通用标准选项；后端 `provider_compat.py` 改造为通用协议层；`README.md` 与文档同步纯净化；全量回归：pytest 270 passed，npm test 92 passed，Playwright e2e 39 passed 全绿。
- 2026-08-21（商业化纯净化与存量 e2e 全通）：按商业化交付标准彻底清理私有域名/测试报告/路径；`provider_compat.py`、`llm.py`、`client.py`、`api_acceptance.py` 常量全接入 `os.getenv` 动态机制；根目录新增 `.env.example`；修复 OPC 双击跳转与 effective timeout 两个存量 e2e，全量 39 个 e2e 首次全绿。

- 2026-08-20（硬编码审查）：全面审查后端和前端代码中的硬编码问题。工具链：grep 搜索 URL/端口/路径/密钥/模型名/超时等模式。发现并分类如下：

  **🔴 值得关注（3 项）**
  -① `backend/agentcad/provider_compat.py:9` —— `KIMI_CODING_BASE_URL = "https://api.kimi.com/coding/v1"` 硬编码为模块级常量，未通过环境变量暴露；Kimi 若迁移 API 地址需改代码。
  -② `backend/agentcad/provider_compat.py:10-17` —— `KIMI_CODING_MODEL_IDS`、`KIMI_K3_MODEL_IDS`、`KIMI_K3_MAX_COMPLETION_TOKENS`(8192)、`KIMI_K3_VISION_MAX_COMPLETION_TOKENS`(16384) 硬编码；模型新增/改名/调限额需改代码。
  -③ `backend/agentcad/api_acceptance.py:19` —— 内嵌 HTML 表单默认值：`base_url="https://apihub.agnes-ai.com/v1"`、`model="agnes-2.0-flash"`、`timeout=120`、`repetitions=3`、`replans=3`；指向特定第三方服务，下线需手动更新。

  **🟡 建议关注（4 项）**
  -④ `backend/agentcad/client.py:32-34` —— Python SDK 默认 `base_url="http://127.0.0.1:8000"`、`timeout=120`；非本地部署每次需传参覆盖，且超时与服务端默认 600s 不一致。
  -⑤ `backend/agentcad/llm.py:447-448` —— 错误消息硬编码 Kimi 模型名和 URL，未引用 `provider_compat.py` 常量，维护时易遗漏。
  -⑥ `frontend/src/providerPresets.ts` —— 7 个预设 Provider 的 Base URL 全部硬编码（OpenAI/Kimi/DeepSeek/OpenRouter/Groq/Ollama/LM Studio），URL 变更需重建前端。
  -⑦ `backend/agentcad/config.py:60,66,72` —— 默认数据库路径/默认 CORS/默认 frontend dist 虽有环境变量覆盖，但 Docker 镜像中已重新指定，三者可能不一致。

  **🟢 可接受（设计上合理的默认值）**：云元数据安全地址、符号文件路径、Dockerfile 端口/Docker-Compose 变量、e2e 测试固定值、build_argon_pid.py 一次性脚本、config.py 的 byte/timeout 默认值（均有统一 _env 机制）。

  **✅ 已正确处理**：前端 API_ROOT（VITE_API_ROOT 可配置）、LLM 连接参数（PID_AGENT_LLM_BASE_URL 等 env 变量）、config.py 全字段（_env + 主/备环境变量名）。

  下一步：① 将 KIMI_CODING_BASE_URL 等常量改为环境变量可覆盖；② 考虑将 providerPresets 通过 API 下发；③ 验收测试页面的默认值抽取到环境变量。

- 2026-08-20（路演 PPT）：pitch/ 生成 18 页青创大赛路演 PPT（依据 P072 项目计划书 docx，嵌入仓库真实插图：氩气 P&ID、阀门图例、e2e 截图；数据口径=模型矩阵 15/15、271 后端测试等）；deck.js 可重建，slides_test 溢出检测通过 + Vision OCR 逐页核验；成品同时复制至 P072/青创大赛/。坑：soffice 需加 PATH；describe_image 本宿主不可用→OCR 替代；已记踩坑日志。下一步：人工审 montage 定稿。

- 2026-08-19（UI 视觉重构）：分支 `ui-polish-2026-08-19` 提交 86710c0——styles.css token 化重写（浅/深双主题）+ 顶栏 ghost 按钮 + 浅色 document-bar + 细滚动条/焦点环统一，导出 PNG 绿边框弱化；10 张视觉快照重生成。门槛：npm test 92✓ / build ✓ / pytest 271✓ / e2e 37✓+2 存量失败（effective timeout 缺 `PID_AGENT_AGENT_TIMEOUT_SECONDS=180`；OPC 双击跳转，基线同样失败）。已合入 main 并推送（a0829dd）。下一步：另开任务修 2 个存量 e2e。

- 2026-08-17（Git 标准化）：工作区统一规范——.gitignore 补全 .reasonix/ 和 credentials*；无业务代码变更

- 2026-08-13：补齐 README 的 Purpose/Status/Stack/Commands/Structure/Configuration/Notes，新增需求与任务文档；确认本机数据库/WAL/SHM 均为 `0600`，未读写数据库或业务代码。
- 2026-08-13：依据 README 与产品边界补齐交接；未改业务代码或数据库。
