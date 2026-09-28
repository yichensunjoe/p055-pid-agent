# M8-Q1 Engineering Coverage 只读运行 · 结果报告

> 日期：2026-09-28 ｜ 代码基线：origin/main @ `1afeccab`（M7-Q2 签 CLOSED 的链）
> 环境：scratch 后端 `127.0.0.1:8002`，一次性库 `data/m8-q1-scratch.db`（跑完即删），真实 TypeSafe key 走服务端环境。
> **零写入边界（对 Gate 声明过的落地解释）**：本次运行全部写入只发生在 scratch DB；product 系统、catalogue、digest、main 分支一律未动。运行结束后 scratch 进程与库文件均已删除（见文末「清场记录」）。
> 执行手段：`reports/m8_q1_runner.py`（8 场景 16 步）+ 失败定位探针（探针结果单独标注，不与冻结 run 混淆）。

## 一、逐场景结果（冻结 run）

| 场景 | 句一 | 句二 | 句三 | 终态 | 期望 vs 实际 |
| --- | --- | --- | --- | --- | --- |
| DEV-1 | ✅ complete (rev1) | ✅ complete (rev2) | — | 3 设备 2 连接 | **符合**：complete 锚点 |
| DEV-2 | 422 receipt `missing_selector:heat_exchanger` | 422 `selector_no_match` | — | 未提交 | 句一符合（歧义 receipt）；**句二不符合**：重述应 selected→complete |
| DEV-3 | ✅ complete (rev1) | 422 布局不可走线 | — | 4 设备 3 连接 | 句一符合；**句二不符合**：PSV 泄放支路无法布局 |
| DEV-4 | 422 `typesafe_spec_partial`（仪表子句被跳过） | blocked（无 base） | — | 未提交 | **不符合**：仪表并列添加未分解 |
| DEV-5 | 422 `selector_no_match:heat_exchanger` | — | — | 未提交 | 不符合（系统子句未走到判定） |
| DEV-6 | 422 `selector_no_match:fractionation_column` | blocked | blocked | 未提交 | 不符合（feed 口 selector 失配） |
| HOLDOUT-1 | 422 `selector_no_match:heat_exchanger` | blocked | — | 未提交 | 句二未执行（依赖句一 committed base） |
| HOLDOUT-2 | 409 布局/文档对账冲突 | blocked | — | 未提交 | 句二未执行 |

失败步的完整响应体另见 `reports/m8-q1-raw.json`。

## 二、四指标

### 1. scenario_complete_rate
- 步级 committed：3/16（DEV-1×2、DEV-3 句一）。
- 场景级全 complete：1/8（DEV-1）；DEV 口径 1/6。

### 2. catalog_gap_convergence（DEV gap 排名，HOLDOUT 只记录不参与）

| # | gap | 命中场景 | 性质 |
| --- | --- | --- | --- |
| G1 | heat_exchanger 管程侧 selector 失配：「管程入口」「管程入」均 `selector_no_match`，而候选端口 tube_in 的 name 就叫「管程入口」、selectors 列了「管程入」——**匹配器没有匹配上自己登记的别名**；「左侧」等方位别名可过语义 | DEV-2、DEV-5、HOLDOUT-1（3/8，最高频） | selector 匹配器缺陷（M7-Q2 契约 m7-port-selector/1 内的回归面） |
| G2 | 仪表并列添加子句不分解：「给 V-101 添加一台液位计 LIT-101、一台压力表 PIT-101 和一台温度变送器 TT-101」被 TypeSafe 合成单 clause「给 液位计 、 压力表 温度变送器」，符号库无候选 → skipped，整句 partial | DEV-4 | planner 子句分解 |
| G3 | fractionation_column feed 口 selector 失配：「原料进料口」对 feed 口（selectors 只有 feed/原料进料） | DEV-6 | 词表缺口（同 G1 族，但「原料进料口」连别名都没登记全） |
| G4 | 安全阀泄放支路布局不可走线：`connection 'cn_5' cannot be routed orthogonally from el_PSV_101.outlet to el_V_101.in without crossing 3 other node bounds` | DEV-3 | auto_layout 绕障（非 catalogue） |
| G5 | DXF 导出不支持 A（椭圆弧）命令：`buffer_tank`、`fractionation_column`、`three_way_control_valve` 的路径含 A，采样器只认 M/L/Q/Z → 含这三类符号的图全部 422 `unsupported_symbol_path` | DEV-1、DEV-3（run 内）；catalogue 扫描确认 3 符号 | catalogue↔导出器契约缺口 |
| G6 | 附带数据质量：shell_in 的 selectors 混入「塔顶/顶部/上面」等塔词汇（DEV-2 receipt 候选可见） | — | catalogue 数据污染 |

### 3. ambiguity_resolution_rate
- 冻结 run 内收到 receipt 后重述 resolve 成功率：**0/1**（DEV-2 句二重述仍 `selector_no_match`，根因 G1）。
- receipt 契约本身质量良好：422 detail 携带 `candidates`（端口全名/方向/介质/侧/selectors）与 `port_selector_contract_version: m7-port-selector/1`，足够支撑用户按候选重述。

### 4. export_readback_quality
- PDF：8/8 全部 200，18.0–20.6 KB。
- DXF：**含实体的导出 0/3 成功**（DEV-1、DEV-3、HOLDOUT-2 均 422，根因 G5）；其余 5 个文档为空（未提交），导出 200 但为 991 B 空壳。即本次 run 没有拿到任何一份含实体的 DXF readback。

## 三、HOLDOUT 独立小节（只记录，不进排名、不反推修复）

- HOLDOUT-1：与 DEV-2/5 同因（G1）止步句一；句二「换成闸阀」因依赖 committed base 未能执行。
- HOLDOUT-2：句一撞布局/文档对账守卫（`el_connector_cn_1/2 was routed differently in the document than the layout decided`），双泵并联汇合拓扑未进入提交。
- 按冻结纪律：以上只记录，不作为 G1–G6 排序或修复依据。

## 四、探针期补充缺陷（冻结 run 之后的定位探针发现，独立标注）

1. **P1 409 先写库后报错（原子性破坏）**：新文档 probe 一句 HOLDOUT-2 句一，返回 409「document does not cover the layout」，但 GET 显示该文档 rev=1、已落 15 图元。对账守卫在写入之后触发，无回滚。
2. **P2 跨文档状态泄漏**：「主工艺系统」（DEV-5 句一的系统声明，该句 422 未提交）出现在之后**所有**成功提交的文档 systems 里（含全新 probe 文档）。疑似进程级共享 registry/缓存未被文档边界隔离。
3. **P3**：方位 selector「左侧」能使 DEV-2 重述通过语义层（G1 的对照证据），但叠加 P1/P2 环境后无法完成提交验证。
4. 探针对 scratch 文档造成污染（DEV-2 文档被探针提交）——不影响冻结 run 数据（raw.json 在探针前已落盘），此处如实声明。

## 五、offline CAD 结构对比（`reports/pid-repro/` 气路系统复现图，定性，非 hard gate）

对比对象是该 CAD 复现（约 9 台主要设备、密集仪表与设备间连线的工业气路图）与本次 8 场景输出：

- **主要设备覆盖**：corpus 场景覆盖罐/泵/换热器/塔/三类阀，与该图设备大类相当；但 cad 图含本次语料未覆盖的机型（如压缩机、大型换热群组），corpus 下阶段可补。
- **连接拓扑**：本次输出为 3–5 节点的单链/简单支路；该图为多设备网状拓扑含汇合/旁路（HOLDOUT-2 的并联汇合即模拟此类，当前布局器未能通过）。
- **仪表密度**：本次 run 仪表挂接 0 成功（DEV-4 未提交），与该图密集仪表圈形成数量级差距，G2 是主因。
- **版面宏观结构**：本次为单排链式自动布局；该图为多排功能分区。当前 auto_layout 是覆盖式整图重排，距离工程图版面习惯尚远（G4 旁证）。

## 六、runner 失败根因（上一轮整批 422 的说明）

上一轮运行每个场景都返回 `422:typesafe_rejected`：runner payload 自带 `"api_key": "x"`，覆盖了服务端环境里的真实 TypeSafe key。已删除两处 `api_key` 字段，本轮认证正常。

## 七、corpus 一句话修正（已落实）

DEV-6 句二改为：「添加一个冷凝器 E-101，把 T-101 的塔顶气相出口接到 E-101 的工艺入口，把 E-101 的工艺出口接到 T-101 的回流入口」（独立 add 子句，原句把 add 藏在连接子句里）。已写入 `reports/m8-corpus-draft.md`。

## 八、清场记录

- scratch 后端（8002）已停止；`data/m8-q1-scratch.db*` 已删除；用户自有服务（8000/5173）未触碰。
- 交付物：`reports/m8-corpus-draft.md`（含本修正）、`reports/m8_q1_runner.py`、`reports/m8-q1-raw.json`、本报告。
