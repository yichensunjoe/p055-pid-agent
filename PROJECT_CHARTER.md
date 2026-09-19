# P&ID-Agent / AgentCAD 总体技术任务书

> 文档性质：项目长期主约束（Canonical Project Charter / Master Task Specification）  
> 当前版本：1.0.0  
> 生效日期：2026-09-18  
> 当前仓库：P055-PID-Agent  
> 长期平台方向：AgentCAD Engineering Drawing Harness  
> 首个生产级领域：P&ID  
> 状态：长期有效；实施路线可迭代，核心目标不得被普通开发任务覆盖

---

## 0. 文档目的

本文件不是一次性路线图，也不是当前版本的功能清单。它用于约束未来所有开发者、AI Agent、自动规划器和更强大模型对本项目的长期演进。

未来随着大模型、视觉理解、CAD 几何算法、工程规则引擎和软件基础设施升级，项目允许重新规划技术路线、重构模块、替换技术栈、调整里程碑和工具粒度；但任何重规划都必须服从以下核心目标：

> 最终构建一个面向工程图纸的 AI Agent Harness，使 AI 通过受控、可验证、可审计的 CAD / Engineering Tools 理解、创建、修改、检查和交付工程级图纸。P&ID 是首个核心领域。最终产物不是示意图、图片或演示稿，而是能够进入正式工程设计审查流程，并在项目规定的专业校核、审核与批准后作为正式工程交付包组成部分使用的结构化图纸和关联工程数据。

本项目的成功标准不是“AI 看起来会画 P&ID”，而是：

1. 工程语义正确；
2. 连接拓扑正确；
3. 图例和位号正确；
4. 工程规则可验证；
5. 图面达到工程制图要求；
6. 图纸与设备表、管线表、仪表索引等关联数据一致；
7. 每次修改可撤销、可追踪、可审计；
8. 输出可进入真实工程工作流；
9. 更换模型后系统仍可工作；
10. 最终能够在真实项目中形成经工程师审查批准的施工阶段交付物。

---

# 第一部分：项目使命与不可变核心

## 1. 项目使命

P&ID-Agent 当前是一套结构化 P&ID 软件。长期将其演进为 AgentCAD：

**AgentCAD = 面向工程图纸的专用 AI Agent Harness。**

类比 coding harness：

~~~text
Coding Harness
Model -> Files / Terminal / Compiler / Tests -> Working Software

AgentCAD
Model -> Engineering Tools / Semantic Model / Validators / Renderer -> Engineering Deliverables
~~~

模型负责理解、计划、选择工具、解释结果和修复问题；确定性系统负责工程对象、连接、规则、版本、审计、布局、导出和质量门。

## 2. 不可变核心原则

以下原则默认为 P0，未来模型不得在普通任务中自行删除。

### P0-1 工程交付优先

不得把“生成一张看起来像 P&ID 的图片”当作完成。必须输出可编辑、可查询、可验证的工程模型及其图纸投影。

### P0-2 LLM 不直接拥有图纸真相

LLM 不得绕过受控接口直接写数据库、直接改持久化状态或用任意代码修改正式工程数据。所有变更必须进入工具或事务层。

### P0-3 语义优先于像素

泵、阀门、仪表、管线、连接点、信号线和跨页接口必须是工程对象，不只是 SVG path、line、circle。

### P0-4 真实拓扑优先

视觉上的相交、接触、重叠不等于工程连接。端口、junction、branch、merge、off-page connector 必须显式建模。

### P0-5 确定性系统包围概率模型

凡可由 schema、类型、规则、几何算法、数据库约束、约束求解或测试确定完成的工作，不应完全依赖大模型猜测。

### P0-6 高风险修改可回滚、可审计、可审批

删除设备、修改关键管径或等级、修改安全功能、改变联锁或正式发布等动作必须有权限和审批边界。

### P0-7 模型可替换

不得绑定某家模型厂商或某一代模型。模型只是可插拔 planner / reasoner。

### P0-8 项目标准高于通用默认

单位图例、项目设计规定、编号规则、图层、标题栏、审批流程、管道等级等项目配置优先。

### P0-9 图纸与工程数据最终一致

P&ID、设备表、管线表、仪表索引、回路数据、I/O 等可关联对象必须避免出现无约束的多份真相。

### P0-10 正式工程责任边界不能伪造

系统可以自动完成大量前置设计、校验和制图，但正式 IFC/AFC/Approved 类状态必须遵循项目要求的专业校核、审核和批准流程，模型不能自行批准或伪造签审。

---

# 第二部分：目标产品形态

## 3. 两层产品架构

长期产品分为两层。

### 3.1 AgentCAD Harness

负责通用能力：

- Session；
- Project Context；
- Model Adapter；
- Tool Registry；
- Permission；
- Approval；
- Plan / Execute / Inspect / Repair Loop；
- Checkpoint；
- Undo / Redo；
- Audit；
- Drawing / Artifact Management；
- Benchmark；
- Observability；
- Domain Package；
- Model Upgrade Replanning。

### 3.2 Engineering Domain Package

P&ID 是第一个 production domain。长期可以有：

~~~text
agentcad/
  runtime/
  domains/
    pid/
    pfd/
    electrical/
    instrumentation/
    loop-diagram/
    isometric/
    plot-plan/
  validators/
  renderers/
  model-adapters/
  project-context/
  cli/
  web/
~~~

在 P&ID 未达到真实工程使用成熟度之前，不应为了“平台化”过度抽象或稀释主线。


# 第三部分：核心架构与工程语义

## 4. 目标架构

~~~text
User / Engineer
      |
      v
AgentCAD Harness
  |-- Session / Context
  |-- Planner
  |-- Tool Registry
  |-- Permissions / Approval
  |-- Checkpoint / Audit
  |
  v
P&ID Domain Tools
  |
  +--> Semantic Engineering Model / IR
  +--> Rules / Validators
  +--> Layout / Routing
  +--> Renderer / Exporter
~~~

当前仓库已有 DocumentService、TransactionRequest、revision、undo/redo、semantic connector、symbol registry、quality harness、REST、MCP 和 Python Client。迁移时应优先复用，而不是大爆炸重写。

## 5. P&ID Semantic IR 是图纸的 AST

长期应把 P&ID IR 当作工程图纸的 AST：

~~~text
P&ID / Project Data
      ->
Semantic Engineering IR
      ->
Renderer / Rule Engine / Reports / Export
~~~

UI、模型、导出器和未来外部集成都不能绕过这一层。

## 6. IR 最低对象模型

### 6.1 Project

至少包括：

- project id；
- project number；
- owner / company；
- plant / unit / area；
- standard profile；
- current phase；
- project revision policy；
- drawing index；
- metadata。

### 6.2 Drawing / Sheet

至少包括：

- drawing number；
- title；
- sheet number；
- revision；
- issue status；
- paper size；
- border；
- title block；
- zones；
- notes；
- referenced drawings。

### 6.3 Equipment

至少包括：

- stable id；
- tag；
- equipment class；
- symbol key；
- service；
- position；
- orientation；
- ports；
- engineering properties；
- linked datasheet；
- provenance。

### 6.4 Port

每个可连接对象必须有明确端口：

- port id；
- parent；
- name；
- connection type；
- direction；
- orientation；
- medium/service compatibility；
- nominal size；
- rating；
- constraints。

### 6.5 Pipeline

不能只用“connector geometry”代表管线。目标 line object 至少包含：

- stable line id；
- line number；
- source；
- target；
- service；
- size；
- spec/class；
- insulation/tracing；
- flow direction；
- route geometry；
- route mode；
- continuation；
- off-page reference；
- provenance。

### 6.6 Junction / Branch / Merge

所有真实分支与汇合必须是显式工程拓扑对象。

### 6.7 Valve

至少包括：

- stable id；
- tag；
- valve type；
- function；
- actuator；
- fail position；
- line association；
- upstream/downstream；
- engineering properties。

### 6.8 Instrument

至少包括：

- tag；
- function；
- loop id；
- measured variable；
- device type；
- location；
- sensing point；
- signal type；
- control relation；
- linked equipment / line；
- optional I/O mapping。

### 6.9 Signal

必须把 process line 和 signal line 分开。支持 electrical、pneumatic、digital、software/data、hydraulic 以及项目自定义类型。

### 6.10 Annotation

至少包括 label、note、dimension、callout、line label、revision cloud、legend reference，并能够和工程对象关联。

### 6.11 Off-page Connector

必须保存 stable connection id、source drawing、target drawing、service/line 和 reciprocal validation。

### 6.12 Revision / Change

每次工程修改至少关联：

- actor；
- model/provider；
- user instruction；
- tool calls；
- before revision；
- after revision；
- change reason；
- validator result；
- approval result；
- timestamp。

---

# 第四部分：Agent Harness 行为

## 7. 标准执行循环

Agent 的复杂任务标准循环：

~~~text
Understand
 -> Inspect
 -> Plan
 -> Execute Tools
 -> Validate
 -> Render / Inspect
 -> Repair or Replan
 -> Human Approval if needed
 -> Commit
 -> Export
~~~

禁止把复杂工程任务长期设计成“一次 prompt -> 一次整图生成”。

### 7.1 Inspect First

修改既有图纸前，Agent 必须读取目标 drawing revision、相关对象、上下游连接、工程属性、当前 issue、project rules 和 symbol constraints。

### 7.2 Plan

多步修改应生成结构化内部计划，至少包含目标对象、影响范围、工具、风险、验证项目和 approval requirement。

### 7.3 Execute

所有修改通过 domain tools。

### 7.4 Validate

每次关键变更后运行相关最小校验；正式交付前运行完整校验。

### 7.5 Inspect Result

transaction 成功不等于工程任务完成。Agent 必须检查 semantic diff、rule issues、render preview、route conflicts、cross-document consistency。

### 7.6 Repair / Replan

validator 返回稳定 issue code 后，Agent 应局部修复，而不是整图重画。


# 第五部分：Tool Registry 与权限

## 8. Tool Registry 设计要求

每个工具必须具备：

- 唯一 name；
- 清晰 description；
- input schema；
- output schema；
- permission level；
- side-effect level；
- precondition；
- postcondition；
- stable error code；
- audit metadata；
- preview 或 dry-run 策略；
- 幂等性定义；
- tests。

## 9. 第一批工具类别

### 9.1 Inspect

- project_open；
- drawing_open；
- drawing_query；
- element_get；
- element_search；
- connection_get；
- upstream_trace；
- downstream_trace；
- loop_trace；
- validation_get；
- diff_get。

### 9.2 Equipment

- equipment_add；
- equipment_update；
- equipment_move；
- equipment_rotate；
- equipment_replace；
- equipment_delete。

### 9.3 Pipeline

- line_add；
- line_connect；
- line_disconnect；
- line_split；
- line_merge；
- line_reroute；
- line_update_size；
- line_update_spec；
- junction_add；
- offpage_connect。

### 9.4 Valve

- valve_add；
- valve_replace；
- valve_update；
- valve_move_inline；
- valve_delete。

### 9.5 Instrument

- instrument_add；
- instrument_attach；
- instrument_update；
- signal_connect；
- loop_create；
- loop_update；
- loop_validate。

### 9.6 Layout

- move；
- align；
- distribute；
- route；
- reroute_region；
- avoid_collision；
- compact_region；
- auto_layout；
- relabel_region。

### 9.7 Annotation

- tag_place；
- line_label_place；
- note_add；
- dimension_add；
- revision_cloud_add；
- callout_add。

### 9.8 Validation

- validate_element；
- validate_topology；
- validate_tags；
- validate_connections；
- validate_layout；
- validate_standards；
- validate_cross_document；
- validate_release。

### 9.9 Export

- render_preview；
- export_svg；
- export_pdf；
- export_dxf；
- export_project_package；
- generate_issue_package。

### 9.10 Revision

- checkpoint；
- diff；
- undo；
- redo；
- rollback；
- create_revision；
- submit_for_review；
- approve_revision；
- reject_revision。

## 10. 权限等级

### Level A — Read Only

查询、报告、预览，默认可自动执行。

### Level B — Draft Edit

移动、排版、局部路由、标注整理等，可自动执行但必须可撤销。

### Level C — Engineering Change

例如修改管径、等级、设备关键属性、阀门类型、process connectivity、control loop、删除工程对象。默认要求项目策略判断或人工确认。

### Level D — Safety / Critical Change

删除或改变安全阀、SIS/ESD、泄放路径、关键隔离方案、联锁等，必须显式审批。

### Level E — Release

Approved、IFC、AFC、正式签发、电子签章等，模型不得自行批准。

## 11. Approval Gate

高风险变更应呈现：

~~~text
proposal
 -> semantic diff
 -> visual diff
 -> engineering impact
 -> validation result
 -> human approve / reject
 -> commit
~~~

用户审批对象应尽量是可读的工程变化，而不是低层 JSON patch。

---

# 第六部分：工程规则、标准和符号

## 12. Rule Engine

规则不能主要存在于 prompt。必须逐步建设独立 rule engine。

规则优先级：

1. system default；
2. industry profile；
3. company standard；
4. project standard；
5. drawing-specific exception。

后层可覆盖前层，但 exception 必须记录 provenance。

规则至少覆盖：

- tag / numbering；
- equipment constraints；
- valve placement；
- instrument connection；
- branch / merge；
- off-page；
- line specification；
- drawing quality；
- title block；
- revision；
- release；
- project-specific rules。

每个 issue 输出：

- issue code；
- severity；
- object ids；
- message；
- expected；
- actual；
- suggested repair；
- rule source；
- waiver status。

## 13. 标准 Profile

系统不能假设一个全球统一 P&ID 标准。项目必须显式配置采用的行业标准、企业标准、项目设计规定、图签规则、图例规则和 revision 规则。

具体标准版本不能让模型凭记忆猜测，应由项目配置或受控知识源提供。

## 14. Symbol Library

符号定义至少包括：

- stable key；
- name；
- category；
- engineering class；
- SVG geometry；
- default bounds；
- ports；
- port direction；
- allowed rotation；
- scale policy；
- editable properties；
- tag rules；
- connection constraints；
- drafting notes；
- version；
- source；
- deprecation metadata。

工程项目必须能够 pin symbol library version。符号升级需要 migration 和 compatibility 策略。


# 第七部分：确定性制图与图纸理解

## 15. Layout / Routing Engine

LLM 可以决定工艺关系和大致布局意图，但最终几何位置、管线路由和标注摆放应逐步交给确定性算法。

长期目标：

- 正交；
- 少折点；
- 避免 micro-segment；
- 避免管线穿设备；
- 避免文字压线；
- 避免图例重叠；
- 保持主流程方向；
- 保持端口方向；
- 支持局部重排；
- 支持人工锁定；
- 支持 routing corridor；
- crossing 与 junction 清晰区分。

route cost 可综合：

- path length；
- bend count；
- obstacle penalty；
- crossing penalty；
- annotation collision；
- port direction penalty；
- corridor preference；
- alignment reward；
- locked-region penalty；
- boundary penalty。

人工已确认区域必须可以 lock，auto-layout 不应随意破坏。

## 16. Existing Drawing Understanding

系统最终必须会读真实已有图纸，而不只是新建。

长期输入范围：

- project JSON；
- SVG；
- DXF；
- vector PDF；
- raster PDF；
- scan/image；
- future DWG interoperability；
- line list；
- equipment list；
- instrument index。

解析目标：

- symbols；
- tags；
- pipelines；
- ports；
- junctions；
- valves；
- instruments；
- signals；
- notes；
- off-page connectors；
- topology；
- sheet metadata。

任何不确定解析结果都应保存 confidence、source region、parser/model、ambiguity 和 human confirmation state，禁止把低置信度猜测伪装为确定工程事实。

低置信度对象进入 review queue，人工确认后再升级为可信语义。

---

# 第八部分：Project Context 与跨文档一致性

## 17. 工程项目目录就是 Agent 上下文

长期目标示意：

~~~text
project/
  project.yaml
  standards/
  symbols/
  drawings/
  equipment/
  piping/
  instruments/
  datasheets/
  line-list/
  instrument-index/
  revisions/
  approvals/
  references/
~~~

Agent 进入项目后应能够读取项目标准、drawing index、equipment list、line list、instrument index、revision history、project exceptions、current issues 和 approval state。

## 18. Engineering Data Graph

P&ID 不应永远是孤立 drawing。长期形成 project engineering graph。

例如：

~~~text
P-101A
  -> PID-001
  -> datasheet DS-P-101A
  -> suction line L-1001
  -> discharge line L-1002
  -> instruments PI-101 / FI-101
~~~

优先关联：

- Drawing <-> Equipment List；
- Drawing <-> Line List；
- Drawing <-> Instrument Index；
- Drawing <-> Loop Data；
- Drawing <-> I/O List；
- Drawing <-> Datasheet reference。

每个字段应定义 authority。不能让两个系统无约束地同时修改同一工程字段。

---

# 第九部分：Revision、Audit 与 Provenance

## 19. Engineering Change Record

每次 Agent 修改必须可以回答：

- 谁提出；
- 谁执行；
- 使用哪个模型；
- 模型/Provider；
- 用户原始要求；
- 读取哪些上下文；
- 调用哪些工具；
- 哪些对象变化；
- before revision；
- after revision；
- validation；
- approval；
- export artifact；
- timestamp。

## 20. Semantic Diff

必须支持工程对象级 diff，例如：

~~~text
+ XV-104 isolation valve
+ XV-105 isolation valve
~ L-1002 route changed
~ L-1002 size DN80 -> DN100
- PI-103
~~~

同时支持 visual diff：added、removed、moved、rerouted、property changed、revision cloud。

---

# 第十部分：工程级交付定义

## 21. 工程级图纸最低维度

### 21.1 Semantic Correctness

- 关键对象有类型；
- 关键连接可查询；
- 无非法 dangling critical reference；
- branch / merge 正确；
- off-page 成对；
- signal 与 process line 不混淆。

### 21.2 Engineering Metadata

- tag 唯一；
- line number 合法；
- equipment tag 合法；
- instrument tag 合法；
- required properties 完整；
- project rules 通过。

### 21.3 Graphical Quality

- 无设备重叠；
- 无非预期穿线；
- 无严重文字压线；
- 主流程清楚；
- 端口方向合理；
- 管线正交；
- crossing/junction 表达正确；
- 图幅和标题栏正确。

### 21.4 Cross-document Consistency

- drawing 与 equipment list 一致；
- drawing 与 line list 一致；
- drawing 与 instrument index 一致；
- off-page reference 一致。

### 21.5 Export Fidelity

PDF/DXF/其他正式格式需验证：

- geometry；
- line style；
- text；
- layer；
- symbols；
- title block；
- paper size；
- revision。

### 21.6 Reviewability

正式交付候选必须可生成：

- issue report；
- semantic diff；
- visual diff；
- validation evidence；
- revision history；
- approval state。

## 22. Release Gates

### Draft

允许不完整，但不得破坏数据完整性。

### Design Review Candidate

要求 schema valid、topology valid、无 critical validator error、基本图面门槛通过。

### Engineering Review Candidate

要求 project rules、cross-document checks、drawing quality、semantic diff、unresolved issue list 完整。

### Issue Candidate

要求 required validators pass、required properties complete、export verification pass、revision metadata complete、reviewer assigned。

### IFC / AFC / Approved Release

必须经过项目规定的人工审批，保存审批身份、时间、正式 revision、release package、hash 和 provenance。模型不得自行把 Draft 变成 Approved。


# 第十一部分：测试、Benchmark 与模型策略

## 23. 测试体系

测试不能只验证 API 返回 200。

### Unit

覆盖 model、routing、rules、symbols、transactions、parser、exporter。

### Integration

覆盖 transaction -> store、tool -> transaction、validator -> repair、project context -> agent、export -> reimport。

### Browser E2E

覆盖人工绘制、Agent 局部修改、undo、revision conflict、approval、export。

### Golden Drawings

长期维护 golden project corpus，至少包括：

1. simple pump line；
2. tank + pump；
3. valve station；
4. heat exchanger loop；
5. instrumented process line；
6. branch / merge；
7. control loop；
8. relief path；
9. multi-sheet off-page；
10. complex real-world style P&ID。

条件允许时引入脱敏真实历史项目。

## 24. 核心质量指标

持续量化：

- topology accuracy；
- symbol correctness；
- tag accuracy；
- connection accuracy；
- routing quality；
- annotation collision rate；
- project-rule pass rate；
- reconstruction accuracy；
- cross-document consistency；
- agent repair success；
- tool success rate；
- undo reliability；
- export fidelity；
- large-drawing performance。

正式 release candidate 至少要求：

- critical topology error = 0；
- duplicate engineering tag = 0；
- broken off-page connection = 0；
- invalid object reference = 0；
- transaction integrity error = 0；
- unresolved release blocker = 0。

当前 diagram-quality 95 分门槛可保留，但不得把单一总分视为工程验收全部。

## 25. Model Adapter

适配层至少处理：

- provider；
- model id；
- context capability；
- vision capability；
- structured output；
- tool calling；
- reasoning；
- token limits；
- retry；
- cancellation；
- streaming。

系统能力基线应主要由 tools + validators 保证，而不是只有某个特定模型才能完成。

不同模型可以采用不同策略：

- small model：更严格工具模板；
- strong model：更高层计划；
- vision model：图纸理解；
- local model：隐私项目；
- future engineering model：更高自主性。

每个新模型至少 benchmark：

- simple generation；
- local edit；
- repair；
- topology reasoning；
- symbol selection；
- large context；
- ambiguous request；
- refusal to bypass approval；
- corrupted drawing recovery。

---

# 第十二部分：未来模型升级后的重规划机制

## 26. Replanning Governance

未来更强模型可以重新制定“可变路线”，但不能静默改变核心。

### 26.1 普通任务不得自动修改

- 项目最终工程交付目标；
- P0 不可变核心；
- human release approval boundary；
- semantic-first；
- tool-mediated editing；
- audit / rollback；
- model-agnostic；
- engineering validation 优先。

这些变更必须由项目 Owner 明确批准。

### 26.2 可以优化

- IR 结构；
- 数据库；
- frontend；
- routing algorithm；
- model orchestration；
- 是否 multi-agent；
- tool 粒度；
- milestone 顺序；
- programming language；
- renderer；
- parser；
- benchmark implementation。

### 26.3 重规划触发

- 新模型 tool use 显著提升；
- vision understanding 大幅提升；
- 新 geometry/CAD 技术成熟；
- 进入真实工程试点；
- 当前架构阻碍质量；
- benchmark 长期停滞；
- Owner 主动要求。

### 26.4 重构方案必须提供

1. 当前瓶颈；
2. 新方案；
3. 对 P0 影响；
4. migration path；
5. benchmark before；
6. expected benchmark after；
7. rollback plan；
8. data compatibility；
9. engineering risk。

不能因为“模型更聪明”就删除 deterministic validator。

## 27. Multi-Agent 原则

初期优先：

~~~text
1 competent agent
+ strong tools
+ semantic IR
+ deterministic validators
+ layout engine
~~~

只有 benchmark 证明单 Agent 有明确瓶颈时，再考虑 Planner、P&ID Designer、Engineering Checker、Drawing QA、Document Controller 等分工。

多 Agent 必须共享结构化工程状态，而不是靠自然语言互聊代替工具和校验。

---

# 第十三部分：性能、安全与真实工程数据

## 28. Large Drawing

真实 P&ID 可能包含数百到数千对象，逐步支持：

- spatial index；
- viewport virtualization；
- incremental render；
- incremental validation；
- region query；
- region reroute；
- partial project loading；
- heavy-task async execution；
- cache；
- scalable undo history。

性能优化不能破坏工程语义。

## 29. Engineering Data Safety

必须保护项目文件、API key、内网模型配置、工程文档和审计记录。

Agent 禁止：

- 绕过 permission；
- 静默大量删除；
- 修改正式 revision；
- 伪造审批；
- 将私有工程数据发送给未批准 provider。

共享部署应支持 provider allowlist、network policy、data boundary、approved-model list 和 request audit。


# 第十四部分：当前仓库迁移计划与里程碑

## 30. 不做大爆炸重写

当前能力作为迁移基础：

- DocumentService；
- TransactionRequest；
- revision；
- undo/redo；
- semantic connector；
- SymbolRegistry；
- engineering reports；
- quality harness；
- REST；
- MCP；
- Python Client；
- PDF/DXF export。

### Phase A

在 transaction 之上加入 Tool Registry：

~~~text
LLM -> PID Tool -> Transaction -> DocumentService
~~~

### Phase B

把现有 document model 逐步升级为更强 domain IR，优先加强 equipment identity、line identity、instrument identity、connection graph、project references、provenance。

### Phase C

把 validator、router、renderer 明确模块化。

### Phase D

加入 harness session、permissions、approval、audit。

### Phase E

P&ID 成熟后再抽取通用 AgentCAD runtime。

---

## 31. Priority 0 — Harness 骨架

### T0.1 Tool Registry

统一工具定义，REST、MCP、内部 Agent 共享同一 schema。

### T0.2 Agent Session

记录 session id、user、project、drawing、model、start revision、tool calls、end revision、status。

### T0.3 Permission / Approval

先实现 allow / ask / deny。

### T0.4 Semantic Diff

工程对象级 diff。

### T0.5 Audit

保存 Agent 工具执行轨迹与结果。

## 32. Priority 1 — P&ID IR

- stable engineering id；
- line object；
- instrument object；
- signal object；
- off-page connection；
- equipment / line / instrument / drawing index。

## 33. Priority 2 — Validator Framework

-统一 validator interface；
- severity：info / warning / error / blocker；
- stable issue code；
- configurable project rules；
- release validator。

## 34. Priority 3 — Deterministic Drafting

- port-aware routing；
- collision detection；
- annotation placement；
- region relayout；
- manual lock；
- crossing / junction semantics。

## 35. Priority 4 — Agent Repair Loop

目标：

~~~text
request
 -> plan
 -> execute
 -> validate
 -> issue codes
 -> repair
 -> validate
 -> preview
~~~

必须用 benchmark 证明 repair loop 比一次性整图生成更可靠。

## 36. Priority 5 — Project Context

建立 project package，纳入 standards、symbols、drawings、lists、references、approval、revisions。

## 37. Priority 6 — Existing Drawing Understanding

建议顺序：

1. structured JSON；
2. SVG；
3. DXF；
4. vector PDF；
5. raster/scanned P&ID；
6. future DWG interoperability。

每一级都建立 reconstruction benchmark。

## 38. Priority 7 — Cross-document Consistency

先实现 drawing -> equipment list、drawing -> line list、drawing -> instrument index 的稳定生成与验证，再决定双向同步。

## 39. Priority 8 — Engineering Review Workflow

实现 draft、review、comments、revision cloud、issue resolution、submit、approve、reject、release package。

## 40. Priority 9 — Engineering Delivery

持续加强 PDF、DXF、project package、drawing index、issue report、revision metadata、export verification，以及根据真实项目需要增加商业 CAD interoperability。

---

# 第十五部分：Definition of Done

## 41. Feature DoD

任何功能不能以“能跑”作为完成，至少满足：

1. 数据模型明确；
2. API / Tool schema 明确；
3. permission level 明确；
4. error semantics 明确；
5. unit tests；
6. integration tests；
7. 至少一个真实 P&ID scenario；
8. undo/revision 行为验证；
9. docs 更新；
10. 影响 Agent 时增加 benchmark；
11. 影响图面时增加 visual/e2e acceptance；
12. 影响工程数据时增加 validator。

## 42. Tool DoD

每个工具必须定义 input、output、permission、mutation scope、precondition、postcondition、error code、dry-run、audit event、tests、example、idempotency。

## 43. Validator DoD

每个 validator 必须 deterministic、stable issue code、可定位 object、severity、rule source、可复现、有 tests，并控制 false positive。

---

# 第十六部分：明确禁止的反模式

## 44. Anti-patterns

禁止以下路线成为长期主架构：

1. 只靠 prompt 解决工程规则；
2. 让 LLM 输出大量绝对坐标作为主要制图机制；
3. 只看截图判断工程正确性；
4. 为展示模型能力而绕过工具；
5. 无 benchmark 地堆 Multi-Agent；
6. 为扩展所有 CAD 场景过早通用化；
7. 用模型置信度代替工程 rule；
8. 正式输出没有 provenance；
9. 因为 validator 难通过而降低工程门槛；
10. 让 Agent 自行批准正式施工 release。

---

# 第十七部分：长期里程碑

## 45. M0 — Structured P&ID Editor

完成标志：structured document、symbols、ports、connectors、transaction、undo、export。

## 46. M1 — P&ID Tool Harness

完成标志：domain tools、tool registry、session、audit、permission 可用。

## 47. M2 — Engineering Semantic Graph

完成标志：equipment / line / valve / instrument / signal / off-page 都有稳定工程身份，project index 可查询。

## 48. M3 — Deterministic Drafting Engine

完成标志：layout、routing、collision、annotation、regional repair、quality gate 达到稳定可重复结果。

## 49. M4 — Engineering Validation System

完成标志：configurable rules、stable issue codes、release validator、project profile。

**M4 完成条件的规范含义**（2026-09-19 由远端 Reviewer / Release Gate 定义，本地不得自行放宽）：

> 仓库只有**一份** canonical、只读的工程校验契约。每条 finding 携带稳定 code、severity
> （`info|warning|error|blocker`）、工程对象 id、图纸元素 id、message、expected/actual、
> suggested repair、rule source、waiver 状态与证据、validator id/version、rule id、profile
> id/version、适用时的**生效阈值**，以及足以复现本次运行的确定性 provenance。
>
> 生效规则链为 `built-in < standard < company < project < release-phase`；非法或未知配置
> **fail closed**。既有 graph / report / drafting validator 是本契约的 **adapter**，不再各自
> 充当对外规则真相。
>
> waiver **标注** finding，永不删除 finding。自动化 release 校验只产出 `eligible|not_eligible`
> 就绪证据，**永远不能**批准/签发/放行图纸。
>
> “同一 canonical payload”有精确含义：机器可读的 REST/CLI/MCP 输出共用**一份公开 schema 与
> 公开字段别名**（`schema` 才是 canonical 字段名），并由一条**完整 payload 等价测试**守住；
> 浏览器 UI 消费的正是这份 REST payload，它只能为显示做排序/过滤，**不得重算策略**，也不得
> 成为第二套校验实现。若某面同时展示校验结果与就绪结论，两者必须绑定同一评估时刻、同一
> revision/profile/规则包，且 `readiness.validation_hash` 必须等于所展示结果的 `result_hash`。
>
> 只有当同一引擎通过 REST/MCP/CLI/UI 一致暴露、read/audit provenance 已绑定、stable
> issue-code 契约由 tests/harness 守住、代表性大图性能已记录、且 backend/frontend/browser/
> shared-mode 全量验收为绿时，M4 才算完成。

M4 未通过 M4-6 验收前，Charter 中不得写入 M4 accepted SHA。

**M4 accepted（2026-09-19，由远端 Release Gate 正式签发）**：

- **M4 accepted HEAD = `ecedc00ae3063a4043334bd30367008d00665a29`**
- M4 acceptance-fix content commit = `b5f2ca5`（R3 §2 必修项内容）
- M4 CI evidence = CI run `35421802528`（success）+ Visual baselines run `35421823171`（success），均绑定 `ecedc00`

`b5f2ca5` 与 `ecedc00` 含义不同：前者是“改了什么”，后者是“被接受的仓库状态”；accepted SHA 固定为 `ecedc00`，不随后续记账类提交而变化。

## 50. M5 — Agent Self-Repair

完成标志：Agent 能依据 validator 局部修复并在 benchmark 达到预设成功率。

## 51. M6 — Existing Drawing Understanding

完成标志：至少一种真实外部 P&ID 格式能够可靠重建 semantic graph，并有 human confirmation workflow。

## 52. M7 — Project Engineering Graph

完成标志：drawing、equipment、line、instrument 数据互相关联，cross-document validator 可用。

## 53. M8 — Engineering Review Workflow

完成标志：comments、revision、approval、release state、evidence package 完整。

## 54. M9 — Construction-grade Delivery Candidate

完成标志：

- 在真实试点项目中，由专业工程师使用本系统完成完整 P&ID 新建或修改；
- 系统 validators 全部通过；
- 外部审查意见可闭环；
- PDF/DXF/项目包满足项目约定；
- revision、audit、approval 完整；
- 经项目规定的人工审核批准后能够进入正式施工交付流程。

## 55. M10 — AgentCAD Platform

只有 P&ID 达到稳定工程使用后推进。完成标志：Harness 与 P&ID domain 解耦，第二个 engineering drawing domain 能复用 runtime，且 P&ID 能力不退化。

---

# 第十八部分：未来 Agent 的强制工作方式

## 56. 每次重大规划前必须读取

1. AGENTS.md；
2. PROJECT_CHARTER.md；
3. HANDOFF.md；
4. docs/product-vision.md；
5. docs/architecture.md；
6. relevant engineering docs；
7. current tests / benchmark；
8. REUSE_AND_PITFALL_LOG.md。

然后回答：

- 当前距离 M9 最近的最大瓶颈是什么；
- 本次任务属于哪个 milestone；
- 是否强化或削弱 P0；
- 如何验证；
- 是否需要 migration；
- 是否影响 engineering data；
- 是否增加或降低 release risk。

## 57. Charter Revision Proposal

未来更强模型认为本文件技术路线过时时，应提交 revision proposal，明确：

- Immutable Core；
- Mutable Architecture；
- Roadmap changes；
- Benchmark impact；
- Migration；
- Owner approval requirement。

Benchmark 可以提高或重新设计，但不能因为难通过而无证据降低。

---

# 第十九部分：最终验收问题

## 58. 项目是否真正成功，最终反复检查以下问题

1. AI 是否理解设备、管线和仪表，而不只是视觉形状？
2. 用户要求在 P-101A 出口增加两个指定阀门时，系统能否做局部工程修改而不是整图重画？
3. 修改后 line list 和 instrument index 是否仍一致？
4. 人工修改后 Agent 是否立即读取最新状态？
5. Agent 是否能解释具体改了什么？
6. 所有修改是否可 undo / rollback？
7. 是否知道哪个工程决定由谁批准？
8. 图纸是否通过 deterministic engineering validators？
9. 图面是否符合项目制图要求？
10. PDF/DXF 是否保真？
11. 工程师是否能继续编辑，而不是只能看图片？
12. 既有 P&ID 是否能重建为语义模型？
13. 大图是否仍可用？
14. 更换模型后系统是否仍工作？
15. 没有模型时，核心工程数据和 validator 是否仍可靠？
16. Agent 是否在不确定工程问题上请求确认而不是编造？
17. 正式发布是否有 review / approval evidence？
18. 是否真正减少工程师重复劳动？
19. 经专业校核和批准后，输出能否成为正式施工交付包的一部分？
20. 如果不能，本轮任务是否在解决最重要的瓶颈？

---

# 第二十部分：长期口号与维护规则

> **LLM does not draw the engineering drawing directly. LLM operates the engineering model through controlled tools.**

> **The drawing is a projection of the engineering model, not the engineering model itself.**

> **A successful demo is not the goal. A reviewable, verifiable, auditable engineering deliverable is the goal.**

## 59. 文档版本规则

建议采用语义版本：

- PATCH：文字澄清；
- MINOR：新增工具、验证要求、里程碑；
- MAJOR：改变核心架构或项目边界。

MAJOR revision 必须：

1. 列出变更；
2. 解释原因；
3. 标明 Immutable Core 是否变化；
4. 获得 Owner 明确批准；
5. 保留 Git 历史。

未来模型可以主动建议更新本文件，但不得静默改变核心目标。

## 60. 当前执行结论

从 1.0.0 开始，本项目研发默认主线为：

**P&ID-Agent 从“AI 可编辑的结构化 P&ID 软件”逐步升级为“面向 P&ID 工程交付的专用 Agent Harness”；P&ID 达到真实工程使用成熟度后，再抽象为 AgentCAD 通用工程图纸 Harness。**

短期不做一次性重写。优先利用现有 DocumentService、Transaction、Symbol Registry、Connector、Quality Harness、REST/MCP/Python 接口，逐步加入：

1. Tool Registry；
2. Session；
3. Permission / Approval；
4. Semantic Diff；
5. Audit / Provenance；
6. 更强 Engineering IR；
7. Validator Framework；
8. Deterministic Layout / Routing；
9. Project Context；
10. Cross-document Engineering Graph；
11. Engineering Review / Release Workflow。

每一阶段必须用 benchmark 和真实工程案例证明它比上一阶段更接近最终工程交付目标。
