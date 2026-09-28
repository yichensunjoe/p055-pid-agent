# M8-Q1R Measurement Integrity Repair · 修复设计（报 Gate 等设计裁定）

> 前置裁定：M8-Q1 = CHANGES REQUIRED；M8-Q1R design = GO；M8-Q2 = BLOCKED；25219f8 直合 main = BLOCKED。
> 本批只做设计与复验，未动主代码。基线 origin/main @ 1afeccab。

## 零、P2 撤回声明（先纠正记录，附证据）

P2「跨文档状态泄漏」经 fresh-scratch 复验为**误报**，证据：

1. fresh DB 顺序执行：C 建空文档 → A 发 422 句（含「添加一个主工艺系统」，selector_no_match）→ D 发 409 句（HOLDOUT-2 双泵并联，撞 layout 对账）→ 复查 C → B 发简单句。
2. 结果：A 失败前后 rev 恒 0、零图元（干净拒绝）；C 全程 rev 0、零图元、systems 仅 Default（**无污染**）；D 的 15 图元只落在 D 自己（rev 1）；B 只含自己句子的 3 符号 1 连接。
3. 「主工艺系统」出现在每个成功提交的文档里，根因是设计默认：`m7_text_planner.py:56-59` `DEFAULT_SYSTEM_NAME = "主工艺系统"`（注释：句子未命名系统时声明的默认系统），`_spec()` 第 671 行无条件写入。不是 DEV-5 泄漏。
4. 我上一批报告把默认系统误读为泄漏，导致裁定里的 P0-2 依据不成立，特此撤回并致歉。

残留处置：P0-2 降为**守卫测试**（A fail→B create、A success→B create 状态断言）并入 P1 的测试矩阵，不设独立修复项；请 Gate 确认是否同意缩小 Q1R 范围。

## 一、P1 原子性（确认的真 P0）

### 根因（已定位到行）
`backend/agentcad/m7_layout_materialization.py` 提交函数（约 1040–1072 行）：
```
result = service.apply_transaction(...)          # 先写库
problems = materialization_matches_document(layout, result.document)
if problems:
    raise MaterializationDocumentError("the committed document does not cover the layout: ...")   # 写完才验，无回滚
```
fresh DB 复验：HOLDOUT-2 句返回 409 后该文档 rev=1、15 图元已落库。**「拒绝」有副作用**，与 M7 文档承诺的 "Refused before the write, so nothing is committed"（同一文件 docstring，第 1040 行附近）直接矛盾。

### 修复设计（两案，推荐 A）
- **案 A（推荐）事务内验后提交**：apply_transaction 与 materialization_matches_document 纳入同一存储事务——先写入、立即在同一事务内对 result.document 做对账、不过则整事务回滚、异常对外。失败路径对外仍 409，但库内零变更。需确认 store 层事务边界（`service.apply_transaction` 的实现是否单事务；若不是，改为传入 staging 副本做投影对账，通过后落库）。
- **案 B 投影预验**：先对深拷贝的 document 应用 materialized_transaction 并跑 materialization_matches_document，通过才真正写库。对账逻辑不变，只是对象从「已落库」换「内存投影」。
- 附加审计：`text-plan`/`text-edit` 的 422 语义拒绝路径（selector_no_match / typesafe_spec_partial / 布局「画不了」，`api_semantic_agent.py:784/833/1035/1090`）逐一核对在写库前终止；fresh DB 已证实 422 路径干净（A 案句 rev 恒 0），但把「GET 前后状态一致」固化成测试。

### 测试矩阵（P1）
| 用例 | 请求 | 断言 |
| --- | --- | --- |
| T1 layout 对账失败 | HOLDOUT-2 句一（fresh doc） | 409 + GET(rev/elements/systems/spec) 与请求前完全一致 |
| T2 布局不可走线 | DEV-3 句二（有 base） | 422 + base 文档逐字节不变 |
| T3 语义拒绝 | DEV-2 句一 / DEV-5 句一 | 422 + 文档 rev 0 不变 |
| T4 参数校验 | base_spec_digest=""（<16 字符） | 422 + 不变 |
| T5 跨文档守卫 | A fail → B create；A success → B create | B 只含自身内容；B.systems 仅 Default+默认系统（撤回项的固化） |

### 预计改动文件
- `backend/agentcad/m7_layout_materialization.py`（提交/对账顺序）
- `backend/agentcad/` store/service 层（仅当事务边界需要，先读后写不改接口）
- `backend/tests/` 新增 P1 回归（T1–T5；后端测试 + 一次端到端画布验收按项目规则）

## 二、G1 selector 自洽（ENGINE BUG，修匹配器，不加 alias 掩盖）

### 契约（按 Gate 冻结）
对每个 declared port：`port.name` 必须 resolve 回自己；`selectors[]` 每个条目必须 resolve 回自己；同一 selector 命中多个 compatible port 时必须显式 ambiguity，不得默认选择。

### 现状证据
tube_in 的 name=「管程入口」、selectors 含「管程入」，但「管程入口」「管程入」均 `selector_no_match`（DEV-2/5/HOLDOUT-1 三场景命中）；「左侧」可过语义。匹配器位置：`backend/agentcad/m7_port_selectors.py`（`parse_selector` 130 / `_port_satisfies` 257 / `resolve_with_selector` 315）。

### 修复设计
- 先写 catalogue-driven contract 测试驱动修复：遍历 SymbolRegistry 全部符号 × 全部 port，断言 name/aliases 自解析；失败清单即修复清单。
- 修复 `parse_selector`/`_port_satisfies` 的匹配逻辑（疑似 name_token 谓词与 alias 整词匹配的比对口径不一致；修复以 contract 测试转绿为准，不靠加 alias）。
- G6（shell_in selectors 混入「塔顶/顶部管口」）按裁定不在 Q1R 修，但 contract 测试会把它显式暴露成「一个 selector 命中多端口」的失败项，登记为后续 catalogue correction 输入。

### 测试矩阵（G1）
| 用例 | 断言 |
| --- | --- |
| T6 全 catalogue 自解析 | ∀port：name→self、∀alias→self |
| T7 重述链路 | DEV-2 句一 receipt → 句二重述「管程入口」→ selected/complete，ambiguity_resolution_rate=1/1 |
| T8 多命中歧义 | 构造同 selector 命中 2 口 → 显式 ambiguity，不默认选 |

### 预计改动文件
- `backend/agentcad/m7_port_selectors.py`
- `backend/tests/`（contract 测试 + T7/T8）

## 三、G2 并列子句分解（PLANNER BUG，DEV-4 原句冻结）

### 现状
「给 V-101 添加一台液位计 LIT-101、一台压力表 PIT-101 和一台温度变送器 TT-101」被合成单 clause「给 液位计 、 压力表 温度变送器」→ 无候选 → skipped → 整句 partial。当前不能得出「仪表 catalogue 缺符号」的结论。

### 修复设计
- 在 `backend/agentcad/m7_text_planner.py`（并列名词短语的 clause 构造处）把「一台 X、一台 Y 和一台 Z」分解为 N 个独立 requested device，共享同一宿主（V-101）。
- 分解后才允许统计各仪表真实 candidate/gap；分解产物进入 clause 账本（receipt 可见）。
- 不改 DEV-4 原句、不改 catalogue。

### 测试矩阵（G2）
| 用例 | 断言 |
| --- | --- |
| T9 DEV-4 句一 | 产出 LIT-101/PIT-101/TT-101 三个独立 requested device（ resolved 或各自 structured gap，不得再合并成单 clause） |
| T10 单仪表句回归 | 「给 V-101 添加一台液位计 LIT-101」行为不变 |

### 预计改动文件
- `backend/agentcad/m7_text_planner.py`
- `backend/tests/`

## 四、G5 DXF 几何契约（选 tessellation，不改 frozen symbol）

### 选择（按 Gate 给的两案）
**采用案 B：DXF 边界做确定性 arc tessellation。** 理由：exporter 只需折线原语；A（椭圆弧）→ 定数等分折线（段数由弧长/曲率确定性推导，同输入同输出），不触碰三个 frozen symbol 的形状数据。

### 实现
- `backend/agentcad/dxf_render.py::_sample_symbol_path`：补 A 命令解析（rx ry xrot large-arc sweep x y），端点参数化后按确定性规则采样折线；其余命令不变。
- 全 catalogue 扫描（40 条 path）作为单元断言：3 条含 A 的 path（buffer_tank / fractionation_column / three_way_control_valve）采样成功。

### 测试矩阵（G5）
| 用例 | 断言 |
| --- | --- |
| T11 catalogue 全量 | 40/40 path 采样无 DxfExportError |
| T12 端到端 | DEV-1 文档 export-v2.dxf → 200 + read_dxf 解析出非空 primitives（含实体 DXF readback PASS ≥1） |

### 预计改动文件
- `backend/agentcad/dxf_render.py`
- `backend/tests/`

## 五、runner 路径修正（进 main 前的小修，Gate 点名）

`reports/m8_q1_runner.py` 两处 `/Users/joe/...` 绝对路径（sys.path.insert 与输出路径）改为仓库相对/参数化；不改 runner 行为。随 Q1R 分支一起，等 evidence PR 流程。

## 六、执行顺序与验证门

1. P1（含 T1–T5）→ 单独跑通后回报；
2. G1 → G2 → G5 可并行开发，各自测试转绿；
3. 全量：后端测试 + 前端构建 + 一次端到端画布验收（项目编码规则）；
4. 冻结 corpus 重跑（同一 scratch process 8 场景 + 跨文档主动检验），对照 Gate 的四项重跑验收（DEV-2=1/1、DEV-4 到独立 candidate、≥1 实体 DXF readback PASS、拒绝前后状态一致；HOLDOUT 只验收）；
5. 重跑证据回报后等 M8-Q1 PASS 裁定 → 才进入 M8-Q2。

## 七、范围外（本次明确不动）

- G3 selector 词表补全、G4 布局绕障、G6 catalogue 数据污染（登记为 Q2 correction 输入）。
- catalogue 不新增 symbol；不改 HOLDOUT 任何句子；不动 corpus。
- m7-semantic-first-synthesis 不合 main；Q1R 工作分支从 origin/main @ 1afeccab 新切。
