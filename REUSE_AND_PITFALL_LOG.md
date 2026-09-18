# REUSE_AND_PITFALL_LOG — P055-PID-Agent


## 2026-09-18 · 审批对象要从 JSON Patch 升级为 Semantic Diff（P055-PID-Agent）

- 场景：Approval Gate 已能严格绑定 exact intent，但工程师若只能看到原始 TransactionRequest / JSON patch，仍然很难判断“到底改了什么工程内容”。
- 结论做法：保留 low-level history snapshot 作为 forensic truth，在其上新增 deterministic Semantic Diff 层，把 symbol/connector/junction/text 等结构化变化解释为 valve/equipment/instrument/pipeline 等工程实体变化，并生成 field-level before/after 与可读 summary。
- 关键经验：工程 Agent 的 review object 应是“工程变化”，不是模型 prompt，也不是 raw JSON；低层 diff 与语义 diff 应分层保存，前者保证精确审计，后者服务审批、人机协作和 Agent repair。
- 风险边界：Semantic Diff 的 risk_hint 只能作为 deterministic review hint；安全等级、SIS/PSV/联锁等正式风险判断必须由项目 Rule Engine 与工程标准决定，不能让启发式分类替代工程规则。
- 实施方式：先提供无写入 preview + revision 持久化 + REST/MCP 共用 contract，再在 T0.5 把 diff hash 和 validation evidence 绑定到 approval/provenance。


## 2026-09-18 · Approval 必须绑定 exact intent，不能只批准工具名（P055-PID-Agent）

- 场景：Tool Registry 已能标记 ask/deny，但若 approval 只记录“用户批准 apply_transaction”，同一个批准令牌就可能被换成另一份事务、另一张图或另一个 Agent session 使用。
- 结论做法：Approval 绑定 `session + tool_name + document_id + canonical intent hash`；执行前重新计算 SHA-256 比对；成功执行后状态变为 consumed，禁止重放。Tool Call 另外记录 base/result revision 和 error status。
- 关键经验：工程 Agent 的“人工确认”不能只是前端按钮状态，必须是后端可验证、持久化、与具体工程变更严格绑定的 capability token。
- 旁路治理：不能只保护高层 semantic endpoint；任何模型可访问的低层 MCP mutation API 也必须复用同一 gate，否则 Agent 会自然选择阻力更小的旧工具绕过治理。
- 人机边界：普通人工编辑器事务仍可直接进入 DocumentService；Agent-originated engineering change 才强制走 Harness Approval Gate，避免把治理层错误地变成所有 UI 点击的额外负担。
- 适用场景：CAD/CAE、数据库变更、基础设施、金融操作等需要“模型可自主规划，但关键写入由人明确批准”的 Agent Harness。


## 2026-09-18 · Tool Registry 先做元数据层，不重写事务执行器（P055-PID-Agent）

- 场景：按 PROJECT_CHARTER Priority 0 启动 Agent Harness 改造；仓库已有成熟的 DocumentService、TransactionRequest、SemanticTransactionCompiler、MCP/REST 能力，若直接重写执行路径风险很高。
- 结论做法：先新增 canonical Tool Registry，只统一机器可读的 schema、permission、risk、side-effect、preview、idempotency、audit event 和 surface；REST、MCP、semantic planner 共用同一 catalog，现有执行器和原子事务边界保持不变。
- 关键经验：Harness 抽象应先“包住”稳定工程内核，再逐步把 permission/session/audit 接入执行门；不要为了架构漂亮而先推翻已验证的事务系统。
- 风险控制：只登记已有真实执行路径的工具；Charter 中尚未实现的理想工具不能先写进 registry 冒充能力。工程变更型 semantic apply 先声明 ask，下一阶段再做 enforcement。
- 验收：增加 registry 单测、REST/semantic schema 同源测试、MCP catalog 同源测试，并由 CI 执行 ruff/quality-harness/pytest/frontend/e2e。

## 2026-09-18 · 用 Canonical Charter 约束未来模型重规划（P055-PID-Agent）

- 场景：项目从“AI 可编辑的结构化 P&ID 软件”进一步明确为面向工程交付的 Agent Harness；未来模型能力会持续升级，单纯维护临时 Roadmap 容易发生目标漂移。
- 结论做法：新增根目录 `PROJECT_CHARTER.md`，把最终工程交付目标和 P0 不可变原则与可变技术路线分离。模型可以优化 IR、工具粒度、数据库、UI、Multi-Agent 等实现，但不能自行取消 semantic-first、受控工具修改、deterministic validation、audit/rollback、model-agnostic 和人工正式 release approval。
- 关键经验：长期 AI 项目不能只写“功能路线图”，还需要明确 immutable core、mutable architecture、benchmark、release gate 和 replanning governance；否则模型越强，越可能高效地偏离最初工程目标。
- 实施原则：先在现有 DocumentService + TransactionRequest 上增加 Tool Registry，不做大爆炸重写；优先完成 Session、Permission/Approval、Semantic Diff、Audit，再逐步升级 Engineering IR 和 project graph。
- 适用场景：任何需要多年迭代、会被多代 Agent 接力开发、同时又有强工程质量/合规责任边界的项目。
- 待同步：工作区总库 REUSE_AND_PITFALL_LOG.md 不在当前 GitHub 仓库连接范围内，本轮仅完成项目内日志。

## 2026-08-21 · 左侧侧边栏双容器滚动冲突根除、全域滚轮穿透与导航标签体系（P055-PID-Agent）

**场景**：用户反馈当图纸较多或点开分类文件夹后，其它文件夹与图纸被向下挤出视口且无法用鼠标滚轮向下滑动，且侧边栏未见清晰可用滚动条。

**结论做法**：
1. **根除嵌套滚动陷阱（Scroll Trap）与全域滚轮穿透**：
   - 彻底移除了 `.document-tree-list` 内部的 `max-height` 和 `overflow-y: auto` 嵌套滚动限制，使图纸目录在侧边栏自然铺展；
   - 将外层 `.sidebar.documents-panel` 设为唯一主滚动容器（`overflow-y: auto` + `scrollbar-gutter: stable` + `scrollbar-width: thin`），并在明暗双主题下配置高对比度滑块，使鼠标在侧边栏任意位置滚动均能平滑穿透滚动到底。
2. **左侧面板导航分栏与一键折叠（`App.tsx` / `DocumentTree.tsx`）**：
   - 顶部提供 `[ 全部 ] [ 📁 图纸 (N) ] [ 📐 图例 ]` 3 档视图切换，支持一键切换到专属满屏图纸管理视图；
   - 工具栏新增 `⊞ 展开全部 / ⊟ 折叠全部` 快捷按钮，支持一键收拢所有分类以便迅速定位目标文件夹。
3. **单位图例收敛与基础图元过滤（`SymbolPalette.tsx`）**：
   - 自动过滤 `category === "基础图元"`，左下角纯净化为工程物项，基础图元由顶部工具栏专职负责。

**验证**：前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，后端 `pytest -q` **271/271 全部通过**，E2E 测试 **42/42 全绿**。

**适用场景**：复杂 CAD 侧边栏多树状节点滚动优化、滚动陷阱消解与分栏导航。

（已同步总库 2026-08-21）

## 2026-08-21 · 大模型流式传输阻塞根因排查与 Starlette ASGI 中间件改造（P055-PID-Agent）

**场景**：用户使用 Ollama / MiniMax M3 Cloud / DeepSeek / Kimi 等模型执行生成时，遇到「额度消耗但无反馈、界面长时间转圈、流式规划未返回有效结果」的严重问题。

**结论做法**：
1. **消除同步阻塞请求与重复调用**：
   - 彻底移除了 `semantic_planner.py` 中流式连接前多余的同步探测性 `client.post`，直接使用单次 `client.stream("POST", ...)` 建立连接，避免首字生成前耗尽额度与产生长时间阻塞。
2. **Starlette BaseHTTPMiddleware 流式冲突与 ASGI 中间件重构**：
   - 排查发现 FastAPI 挂载的 `BaseHTTPMiddleware`（如 `@app.middleware("http")`）在 `StreamingResponse` 进行 `listen_for_disconnect` 时，因底层 TaskGroup 协程读写冲突会抛出 `RuntimeError: Unexpected message received: http.request`，直接强行切断 SSE 连接并导致前端 500。
   - 将 `RequestDiagnosticsMiddleware` 与 `RequestBoundary` 全面重构为原生纯 ASGI 中间件（`async def __call__(self, scope, receive, send)`），彻底根除 TaskGroup 冲突与伪 `http.request` 重复投递。
3. **主流大模型思考链多协议统一分流**：
   - 针对 Ollama / MiniMax 的 `"reasoning"`、DeepSeek / GLM 的 `"reasoning_content"`、OpenRouter 的 `"reasoning"` 以及内嵌 `<think>...</think>` / `<thought>...</thought>`，统一由状态机分流至 `thinking` SSE 通道，实现秒级逐字打字涌现。

**验证**：使用本地 Ollama 与 MiniMax M3 Cloud 真实联调端到端 P&ID 出图流式测试，100% 成功生成储罐与离心泵标准对齐管线，质量评分 100.0 分；后端 `pytest -q` 271/271 全部通过，前端 `npm test` 98/98 全部通过。

**适用场景**：FastAPI / Starlette SSE 流式端点与全局安全/诊断中间件集成、主流推理模型思维链统一分流提取。

（已同步总库 2026-08-21）

## 2026-08-21 · 基础图元顶部分类快捷栏（免属性弹窗）与大模型思考/生成双流式传输（P055-PID-Agent）

**场景**：用户提出两项工程交互增强需求：① 基础图元（如云线、六边形、八边形、菱形、圆柱、机柜、气泡、粗箭头等）不同于复杂工业设备/阀门，放置时无需弹出工程属性配置弹窗；且希望将其置于顶部工具栏分类收纳，支持直接拖拽至下方画布；② 在大语言模型执行自然语言规划时，希望能够实时流式看到模型的深度思考过程（Chain-of-Thought / Reasoning）与输出草案内容。

**结论做法**：
1. **基础图元免属性弹窗与顶部菜单栏分类快捷工具栏（`BasicShapesToolbar.tsx` / `engineeringProperties.ts`）**：
   - 在 `engineeringProperties.ts` 中实现 `isBasicShapeSymbol()` 判定；在 `EditorCanvas.tsx` 中当检测到基础图元时跳过 `requestItemProperties`，拖入/点击放置立即直接落图。
   - 顶部工具栏新增 `BasicShapesToolbar` 下拉分组菜单：分为「几何逻辑」、「设备容器」、「标注流向」、「管道附件」4 个子分类；每个图元卡片均设置 `draggable={true}`，支持鼠标直接拖拽放置到下方画布，或点击选中后单击画布落图。
2. **大语言模型思考过程与输出流式传输（`plan-v2-stream` / `AgentStreamingViewer.tsx`）**：
   - 后端新增 `POST /documents/{document_id}/agent/plan-v2-stream` SSE 端点，在 `SemanticAgentPlanner` 中实现 `_stream_model_json` 与 `stream_plan_events`；同时兼容提取 `delta.reasoning_content`（DeepSeek-R1 / Qwen 等）和 `<think>...</think>` 内嵌思考标签。
   - 前端新增 `api.planSemanticAgentStream` 与 `AgentStreamingViewer.tsx` 响应式流式组件，以双折叠卡片（🧠 思考过程 / 📝 生成草案）配合打字光标和呼吸指示灯实时呈现模型思维链。
3. **踩坑点**：
   - SSE 流式端点须加入 `security.py` 的 `_is_agent_planning_path` 白名单，确保鉴权与生命周期管理一致。
   - Playwright 测试拦截规划请求时，路由匹配规则应使用 `**/agent/plan-v2*` 通配符兼容普通请求与流式请求。

**验证**：后端 `pytest -q` **271/271 全部通过**，前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，Playwright E2E 全量测试 `npm run test:e2e` **42/42 100% 全绿**（含新增 `basic-shapes.spec.ts`）。

**适用场景**：CAD/绘图软件顶部快捷图元分类栏、免属性快速落图交互、LLM Agent 推理流与思考过程实时可视化。

（已同步总库 2026-08-21）

## 2026-08-21 · 项目分类文件夹树状图纸归档与通用基础图元库移植（P055-PID-Agent）

**场景**：用户反映散落的图纸过多无法分清归属项目，要求在左侧支持新建项目文件夹（分类）并按装置/工段/系统归档管理图纸；同时从 draw.io 移植通用工业常用基础图元（变更云线、六边形位号框、八边形安全框、菱形判定框、立式圆柱体、立方体机柜撬块、梯形下料槽、平行四边形IO框、引线说明气泡、工艺粗指示箭头、8字盲板、阻火器、管道视镜等）。

**结论做法**：
1. **项目分类文件夹体系与树状图纸管理（`DocumentTree.tsx` / `FolderDialogs.tsx`）**：
   - 文件夹列表维护在 `projectSettings.metadata.folders`（`id`, `name`, `created_at`）中，天然随项目包导入/导出持久化；图纸归属记录于 `Document.metadata.folder_id`。
   - 左侧侧边栏引入 `DocumentTree` 分组手风琴折叠树：支持一键新建分类、重命名分类、删除分类（关联图纸自动转入「未分类」）、分类内图纸数量 Badge、以及下拉框一键跨文件夹快速移动。
   - 新建图纸弹窗（`CreateDocumentDialog.tsx`）支持直接下拉指定所属项目分类。
   - 顶部集成实时双向搜索过滤框（支持模糊搜索图纸名与文件夹名）与一键清空 `✕`。
2. **draw.io 核心基础图元库移植（`standard_symbols.json`）**：
   - 新增 `"基础图元"` 顶层分类，涵盖变更云线 `revision_cloud`、六边形位号框 `hexagon_tag`、八边形安全框 `octagon_box`、菱形判定框 `diamond_decision`、立式圆柱体 `cylinder_vessel`、立体机柜撬块 `cube_cabinet`、梯形沉降槽 `trapezoid_hopper`、平行四边形IO框 `parallelogram_io`、引线标注气泡 `callout_bubble`、工艺粗指示箭头 `block_arrow_right`，以及管道附件 8字盲板通/断、防爆阻火器、管道视镜等。
   - 所有图元严格符合 Quality Harness 的 `_SUPPORTED_SHAPES` 闭合路径规范与端口流向规范。
3. **踩坑点**：
   - `DocumentSummary` 模型需显式返回 `metadata: dict[str, Any]`，以便前端 `/documents` 列表接口单次请求即可直接获得每张图纸的 `folder_id`，无需对每张图纸发起单独的详情查询。
   - 文件夹创建/重命名弹窗严格遵循项目 SOP 规范，禁止使用原生的 `window.prompt()`，全面采用应用内受控对话框（`FolderDialogs.tsx`）。

**验证**：后端 `pytest -q` **271/271 全部通过**，前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，Playwright E2E 全量测试 `npm run test:e2e` **41/41 100% 全绿**（含新增 `project-folders.spec.ts`）。

**适用场景**：多图纸大型工程项目分类归档树、文件管理交互设计、通用工程几何图元库扩充。

（已同步总库 2026-08-21）

## 2026-08-21 · 属性面板三段式架构与图例库悬停放大镜预览（P055-PID-Agent）

**场景**：借鉴 draw.io 优秀的 Format Panel 与图例 Sidebar 交互，完成右侧属性面板三段式分类（`全部` / `⚙️ 工程` / `🎨 样式` / `📐 排列`）重构，并在排列面板中集成快捷对齐、等间距分布、编组与编辑锁；左侧图例库新增悬停 Popover 放大预览、分类徽章、尺寸与端口清单展示，并为图例搜索框添加一键清空按钮。

**结论做法**：
1. **右侧属性面板三段式重构（`PropertyInspector.tsx`）**：
   - 顶部提供 `[ 全部 ] [ ⚙️ 工程 ] [ 🎨 样式 ] [ 📐 排列 ]` 4 档快速视图切换；默认「全部」视图呈现完整参数卡片栈，点击单个分类可聚焦对应工程/样式/几何视图。
   - 排列面板中集成多选快捷对齐（左/中/右/顶/中/底）、等间距均匀分布（水平/垂直）、编组/解组与图层锁定按钮，一键通过原子事务 `update_element` 提交更新。
2. **左侧图例库悬停放大预览与搜索优化（`SymbolPalette.tsx`）**：
   - 鼠标悬停图例卡片时，经过 350ms 防抖自动弹出高清 Popover 预览卡片，包含大号矢量预览、物项类型 Badge、标准尺寸、端口方向与介质清单，移出即销毁。
   - 搜索框内嵌快捷 `✕` 一键清空按钮，提升搜索筛选效率。
3. **踩坑点**：
   - 属性面板内部分类按钮勿使用 `role="tab"`，避免与页面顶层 5 大主面板 Tab（`[role="tab"]` `属性 1` 等）产生 Playwright Strict Mode 定位冲突。
   - 属性面板使用条件分块渲染与全量 `all` 默认态，既保障单选/多选表单数据统一无漏提交，又完全兼容自动化测试对隐藏输入框的访问。

**验证**：前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，Playwright `npx playwright test` **40/40 100% 全绿**，后端 `pytest -q` **271/271 全部通过**。

**适用场景**：复杂 CAD 属性面板三段式布局、图元悬停放大与端口提示、表单无缝原子持久化。

（已同步总库 2026-08-21）

## 2026-08-21 · 工业标准图元库扩充与智能磁吸等间距吸附体系（P055-PID-Agent）

**场景**：吸收 draw.io 优秀图例与交互算法体系，扩充化工与流程工业核心标准图元（如角阀、三通调节阀、减压阀、螺旋板换热器、精馏塔、旋风分离器、袋式过滤器、螺杆泵、罗茨风机、文丘里混合器），并增强管线跨线桥控制与画布智能磁吸对齐（Smart Guides / 等间距吸附 / 放置吸附）。

**结论做法**：
1. **标准图元库扩充（`standard_symbols.json`）**：
   - 提取并新增 10 个高频工业图元，严格遵循 `SymbolDefinition` 规范，所有闭合截面使用规范 `path`（`d="M ... L ... Z"`）而非非标准的 `polygon`，确保与 Quality Harness、DXF 分层导出、CairoSVG 渲染 100% 兼容。
   - 所有端口（`ports`）设置严格的方向性（`in` / `out` / `bidirectional`）与介质类型（`process` / `drain` 等）。
2. **智能磁吸与等间距吸附（`editorGeometry.ts`）**：
   - `snapSelectionToGuides` 新增多目标中心间的等间距候选计算（`source: "equidistant"`），当移动元件到两相邻元件的等距位置（`dist(A, B) == dist(B, C)`）时，自动触发磁吸并以翠绿色（`#10b981`）呈现等间距辅助参考线。
   - 新增 `snapPointToGuides(point, targetRects, tolerance)`，在图元拖入画布放置（`onCanvasDrop`）时即时磁吸到已有设备中心或边缘。
3. **踩坑点**：
   - AgentCAD 后端 `quality_harness.py` 对图元形状有严格的 `_SUPPORTED_SHAPES = {"line", "polyline", "rect", "circle", "path", "text"}` 静态白名单检查，图元形状切勿使用 `polygon`，应使用标准 SVG `path`。

**验证**：后端 `pytest -q` **271/271 全部通过**，`ruff check agentcad` 0 报错，前端 `npm test` **98/98 全部通过**，Playwright e2e `npm run test:e2e` **40/40 100% 全绿**。

**适用场景**：P&ID 标准图例扩展、CAD 智能参考线与等间距吸附算法、端到端图形质量门禁。

（已同步总库 2026-08-21）

## 2026-08-21 · 物项专属工程属性弹窗与下拉自填配置体系（P055-PID-Agent）

**场景**：用户拖拽或点击放置不同物项（如阀门、设备、管线、仪表、管件/跨图等）到 P&ID 画布时，需要弹出分类专属的工程属性配置对话框；涵盖出入口管径（DN50/DN100/1/4"等标准）、公称压力、材质、故障安全位置（FC/FO/FL/FI）、常态位置（NO/NC/CSO/CSC）、量程与信号制式、设计参数等；所有字段支持广域预设下拉与自由自填兼备（非必填，可一键跳过），并在右侧属性面板实时同步与修改。

**结论做法**：
1. **分类专属 Schema 与标准预设库**（`engineeringProperties.ts`）：
   - 基于 GB/T、HG/T 20559、ASME B16.5、ISA 5.1 标准，建立 5 大类专属 Schema（`valve` 阀门、`equipment` 过程设备、`connector` 工艺管线、`instrument` 过程仪表、`node_fitting` 管件/跨图）。
   - 预设涵盖公称通径（DN10 ~ DN500，1/8" ~ 20"）、压力等级（PN10 ~ PN100，Class 150 ~ 900）、材质（304/316L/碳钢/钛/哈氏合金/PTFE 等）、驱动与故障位置、信号制式（4-20mA+HART/总线）等。
2. **受控弹窗宿主与 Combo-box 交互**（`ItemPropertyDialogHost.tsx`）：
   - 拖拽与点选符号放置时唤起受控弹窗，展示分类 Badge 徽章、位号输入、专属参数双列网格。
   - 所有字段使用 HTML5 `<input list="...">` + `<datalist>` 兼顾模糊下拉预设与自由输入自填，支持「直接跳过」、「确认并放置」与「取消」。
3. **右侧属性栏双向同步与原子持久化**（`PropertyInspector.tsx`）：
   - 元素选中时根据类别自动渲染专属工程参数组，修改通过 `buildPropertyPatch` 将属性结构化存入 `element.properties` 与通用兼容 `element.metadata`。
4. **踩坑点**：
   - `<input list="...">` 元素在无显式 `htmlFor` 时，Playwright 默认角色为 `combobox` 而非 `textbox`，应使用 `getByLabel` 定位，并通过 `<label htmlFor="...">` 与 `<input id="...">` 建立显式关联以增强可访问性。
   - 弹窗确认按钮与取消按钮的 accessible name 避免包含相同子串（如「取消放置」包含「放置」可能引起 Playwright strict mode 冲突），分别命名为「取消」、「直接跳过」、「确认并放置」。

**验证**：前端单测 `npm test` 96/96 通过，Playwright 端到端测试 `npm run test:e2e` **40/40 100% 全绿**（含新增 `item-properties.spec.ts`），后端 `pytest` 271/271 全部通过，`ruff check agentcad` 0 报错。

**适用场景**：工程 CAD/P&ID 专属物项参数录入、HTML5 原生下拉与自填组合控件、前端受控表单弹窗体系。

（已同步总库 2026-08-21）

## 2026-08-21 · 取消超时硬顶与随时手动叫停机制落地（P055-PID-Agent）

**场景**：本地大模型（如 Ollama、LM Studio 或私有部署量化模型）推理与规划耗时较长，原有的 120s/600s 超时上限会导致本地大模型规划经常被系统截断中断；用户要求取消超时设置，改为默认一直持续，同时新增各操作的即时「停止 / 叫停」按钮供随时人工干预。

**结论做法**：
1. **取消超时硬顶，默认无限持续**：
   - 后端 `Settings` 将 `agent_timeout_seconds` 默认值设为 `None`（取消 `<= 600` 的强制硬编码检查，允许环境变量指定或留空表示无限等待）。
   - `ProviderConfig.timeout_seconds` 设为可选 `float | None`，`httpx.Client(timeout=provider.timeout_seconds)` 当 timeout 为 None 时默认不中断连接，满足本地大模型长生成需求。
   - 前端从 Agent 高级设置中彻底移除数字超时输入框与有效上限显示，不强加硬性截断。
2. **全链路 AbortController 与即时叫停交互**：
   - 前端 `api.ts` 的 `planSemanticAgent`、`replanSemanticAgent`、`testProvider`、`listProviderModels` 统一接收 `signal?: AbortSignal`，在 `authorizedFetch` 中监听并在捕获到 `AbortError` 时平滑转为用户友好的取消响应。
   - 手动模式生成中（`planningAgent`/`repairingAgent`）与自动执行中（`AutomaticAgentRunner`）均提供醒目的「🛑 停止生成」/「🛑 叫停」按钮，点击立即调用 `abortController.abort()` 释放连接，清空加载态并优雅提示「已手动停止生成」。

**验证**：后端 `pytest` 271/271 全部通过，`ruff check backend` 0 报错，前端 `npm test` 92/92 通过，`npm run build` & `build:e2e` 通过，Playwright e2e `npm run test:e2e` **39/39 100% 全绿**。

**适用场景**：本地慢速大模型接入、长时 Agent 规划任务、前端 fetch 实时取消（AbortController）与优雅状态恢复。

（已同步总库 2026-08-21）

## 2026-08-21 · 零个人配置商业化交付与厂商解耦纯净化（P055-PID-Agent）

**场景**：彻底完成商业化纯净化改造（零个人配置交付，任何用户部署/开箱即用，动态获取模型，彻底移除第三方私有域名、具体厂商名如 Kimi/DeepSeek/Groq 等硬编码 Preset 与默认模型名，源码与文档 100% 厂商中立），并修复存量 e2e 测试（effective timeout 180s 校验与 OPC 双击跳转）。

**结论做法**：
1. **零默认模型与 100% 厂商解耦标准**：
   - 彻底废除源码中的任何具体厂商命名（如 Kimi）与预设模型（如 `kimi-for-coding`），`providerPresets.ts` 仅保留标准「OpenAI 兼容端点」、「Ollama 本地服务」、「LM Studio 本地服务」、「自定义端点」，且 `defaultModel` 全部置空/移除。
   - 所有外部 LLM 端点、Token 限制、推理模型判定全部转为标准通用协议与环境变量配置（如 `PID_AGENT_REASONING_MODELS`），前端通过 `/models` 动态发现模型或用户手动输入。
   - 提供标准化 `.env.example`，文档中将专有厂商教程替换为通用的 OpenAI-compatible 规范。
2. **Playwright 双击事件在有副作用的单次 handler 下失效**：双击操作（`dblclick`）会在首次 click 时触发 `onClick`。如果 `onClick` 触发了局部状态改变（如选中元素导致侧边栏/属性栏展开，引起 SVG 画布容器 resize 或微位移），第二次 click 会落在屏幕不同坐标上，浏览器将无法聚合产生 `dblclick` 原生事件。**做法**：对于需要支持双击跳转或编辑的透明命中层（如 OPC jump target），单次点击不要触发改变布局的副作用，让双击与捕获监听器纯净触发；同时 JSX 文本中不要把 `&` 误写为 `&amp;`（React 会渲染字面量 `&amp;` 导致选择器失配）。
3. **Playwright webServer 环境变量注入**：`document-creation.spec.ts` 会校验服务端的有效超时配置，需在 `playwright.config.ts` 的 `webServer.env` 中显式注入 `PID_AGENT_AGENT_TIMEOUT_SECONDS: "180"`。

**验证**：后端 `pytest` 270/270 通过，`ruff check` 0 报错，前端 `npm test` 92/92 通过，`npm run build:e2e` 通过，全量 e2e 测试 `npm run test:e2e` **39/39 首次全绿**（含 10 张视觉快照回归），全局 `workspace-check.sh` 0 error。

**适用场景**：商业化项目交付纯净化、LLM 协议通用化与厂商解耦、Playwright e2e 双击交互排障、React SVG overlay 事件模型设计。

（已同步总库 2026-08-21）

## 2026-08-20 · 青创大赛路演 PPT 生成与"无视觉模型"验证法（P055-PID-Agent）

**场景**：依据项目计划书 docx 用 PptxGenJS 生成 18 页路演 PPT（深色科技风），嵌入氩气 P&ID、国标阀门图例与编辑器 e2e 截图；本会话模型（deepseek-v4-pro）无图像输入，且宿主 describe-image 未配置视觉模型 baseURL，无法直接看图。

**结论做法**：
- PPT 生成链路：pitch/deck.js（pptxgenjs + slides skill 的 helpers：imageSizingContain / warnIfSlideHasOverlaps / warnIfSlideElementsOutOfBounds）→ LibreOffice 渲染逐页 PNG（render_slides.py，注意本机 soffice 不在 PATH，需 PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"）→ slides_test.py 溢出检测 → 用 macOS Vision 框架写 swift OCR 脚本逐页核验文字 → PIL 逐行像素高度验证 chip 单行渲染。
- 中文字号换行估算：CJK=1.0 单位、ASCII=0.62、空格=0.35，宽度=(units*pt/72)；chips 文本需留 0.42in 内边距并避免" / "空格连排，否则窄药丸内折行溢出。
- OCR 验证清单：每页标题/关键数字/里程碑标签（M1-M5）/底部技术栈 chips 全部命中；light 文字(>160 灰)与面板边框(<150 灰)用不同阈值区分行高。

**踩坑点**：
- **write 工具写 JS 时模板字面量会吃掉 \n 转义**：deck.js 内含 '\n' 的字符串被写成真实换行导致 SyntaxError，需事后 edit 修补或写入时用 \\n。
- **imageSizingContain 返回不带 path**，addImage 需自行拼 { path, ...contain }。
- **warnIfSlideHasOverlaps 对"文字压面板/圆点压✓"必报 severe**：属正常卡片设计，在代码处注释"有意设计"即可，真正的文字互压（声明盒相交）才需修。
- **describe_image 在本宿主不可用**（describe-image baseURL 未配置），read_image 因模型非多模态被拒；本机验证方案=OCR+像素统计，能覆盖"字在不在/是否折行/是否越界"，但不能审美判断，最终版式需人工过一遍 montage.png。
- **fonts**：全篇指定 Microsoft YaHei（Windows/WPS 命中）；macOS LibreOffice 渲染自动替代为冬青黑体，全角等宽版式不变。

**适用场景**：无视觉能力的会话里生成并自检 PPT/海报等图文产物；中文 PPT 换行与 chip 宽度估算；slides skill 的渲染/校验脚本在本机的 PATH 与字体坑。

（待同步总库）


# 本项目踩坑记录（REUSE_AND_PITFALL_LOG）

本项目专属的踩坑/可复用经验明细。总项目库 `/Users/joe/ai/reasonix/REUSE_AND_PITFALL_LOG.md` 为全局汇总。

规则：执行任务前先读本文件；任务完成后**双写**——先追加本文件，再同步追加总库（见 AGENTS.md）。

已有记录：

## 2026-08-19 · UI token 化重构与 e2e 环境坑（P055-PID-Agent）

**场景**：分支 `ui-polish-2026-08-19` 对前端做整体视觉重构（美观简洁大方），只改 CSS 不动 JSX/TS，并保持浅/深双主题与 e2e 视觉回归通过。

**结论做法**：
- 重构策略：`styles.css` 整体重写为设计 token 驱动——`:root` 定义浅色 token、`.app-shell[data-theme="dark"]` 覆盖为深色 token；各文件早已引用但从未定义的 `var(--border)/var(--panel-background)/var(--muted-text)` 等变量顺带生效。工程画布配色（#69778a/#9ba8b8 与 SVG 元素色）严格不动。
- 验收：临时 Playwright 截图脚本（浅/深/命令面板/Agent 面板四张）人工比对 → `test:e2e:update` 重生成 10 张快照 → 全量 e2e。

**踩坑点**：
- **Playwright webServer 端口冲突要杀子进程**：`kill` npm/uvicorn 包装进程后，vite preview 与 uvicorn 的子进程仍占着 4173/8000，`reuseExistingServer:false` 直接报错；必须 `lsof -nP -iTCP:8000 -sTCP:LISTEN` 找到真实 PID 再杀。
- **Playwright 浏览器版本严格绑定**：本机缓存有 chromium-1223/1234，但项目 @playwright/test 1.61.1 只认 1228，需 `npx playwright install chromium`；临时截图脚本可用 `executablePath` 指向其他缓存版本应急。
- 临时 node 脚本放 `/tmp` 会 ERR_MODULE_NOT_FOUND（ESM 从脚本所在目录解析依赖），必须放进 frontend/ 目录内。
- e2e 两个存量失败（基线同样失败，与 CSS 无关）：`document-creation`「effective timeout」期望上限 180s，但 playwright.config.ts 的 webServer env 未设 `PID_AGENT_AGENT_TIMEOUT_SECONDS`（后端默认 600）；`flow-runtime`「OPC double click」跳转失败根因未查。UI 改动验收前先跑基线对照，避免把存量失败算到自己头上。

**适用场景**：大型 CSS token 化重构、Playwright webServer/浏览器版本排障、UI 回归的基线对照方法。

（已同步总库 2026-08-19）

## 2026-08-06 · 全量 review + 修复后提交（P055-PID-Agent）

**场景**：接管 main 分支 63 文件未提交修改（+5118/−373，codex_handoff 称"已准备推送"但实际未提交），两轮 review 子代理 + 未跟踪新文件审查 + 人工验证，修复 4 中危 + 7 低危后分组提交。

**结论做法**：
- 中危修复：`diagram_quality.py` ① `_element_rect` 旋转符号用绕中心旋转的精确包围盒（0/180° 快速路径）；② `_segment_intersects_rect` 非正交段用 Liang-Barsky 裁剪（要求 t1-t0>EPSILON，保持"严格穿过"语义）；③ `route_connector_points` 交叉计数用按主轴排序的一维段索引 + 二分定位（预排除共享端点连接），`_port_direction`/`infer_flow_direction` 增加 element_map 参数复用；④ `svg.py` `embedded_off_page_label` 坐标由硬编码 (47,30) 改为按定义尺寸推导。
- 低危：`_expand_compact_cabinet_branches` 画布底部空间不足时整体上移组而非压缩高度（8 分支场景不再重叠溢出）；`_place_compact_nodes` 候选枚举上限=到画布边界最大距离；`_request_model_json` 用显式 `repair` 参数替代 `user_prompt.startswith("Schema repair attempt:")` 前缀判定（基类调用点传 repair=True）；compiler polish 异常 `pass` → `logging.warning`；`deleted_text_ids` 排序；前端 renameCurrentDocument 补 catch（`messageFromError` 从 store.ts 导出）。
- 提交分组：feat:quality+标准阀门图例 / feat:vision+SVG+agent runtime / feat:frontend / chore:规则与脚本。

**踩坑点**：
- **本机没有 vitest**，`npx vitest run` 会临时拉取 vitest 4 且与项目 `node:test` 风格测试不兼容 → 33 文件全报 "No test suite found"。项目前端测试必须用 `npm test`（`node --experimental-strip-types --test tests/*.test.ts`）。
- review 子代理环境无 git diff/pytest 权限，只能精读文件；未跟踪新文件（diagram_quality.py 等）review 工具看不到，需另开子代理审查。
- `svg.py` 元素 label 的旋转抵消 `rotate(-θ, anchor)` 以锚点为旋转中心时，位置跟随符号旋转且文本保持正立——绕自身抵消是正确模式；embedded label 旧硬编码 (47,30) 与定义中心 (50,25) 不一致导致旋转 180° 偏移 (6,−10)。
- 旋转包围盒测试用例几何要先手算验证（首版对角段 (50,50)→(400,400) 实际不穿过矩形 (200,100)-(260,140)，测试失败是用例错误而非实现错误）。
- 显式参数重构会破坏直接调用私有方法的测试（test_k3_schema_repair 用字符串前缀模拟契约），需同步更新测试。
- `multi_edit` 跨文件编辑（semantic_compiler_engine + annotation_layout 混在一起）因目标文件错误整体回滚——**multi_edit 只用于单文件**，跨文件用并行 edit_file。

**适用场景**：大型未提交工作批次接管、绘图质量检查模块几何正确性、前端 node:test 测试环境识别。

（已同步总库 2026-08-06）
