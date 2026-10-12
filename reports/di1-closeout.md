# DI-1 Closeout：Drawing Intelligence Iteration-1 收口报告

Charter §55D（v1.3.0）。定义 `reports/di1-definition.md`（Gate 2026-10-12 APPROVED）；设计 `reports/di1-d2-design.md`（Design PASS）；D3 基线 `reports/di1-d3-baseline.md`；D4 实现证据 `reports/di1-d4-evidence.md`；本文件 = D5 评测执行 + F4 冻结 + 非回归 + closeout。

## 完成标志矩阵（F1–F5）

| # | 标志 | 状态 | 证据 |
|---|---|---|---|
| F1 | 代表性场景集（5–10 个，五类覆盖） | **成立** | S1–S6 冻结语料（`backend/tests/di1_corpus/corpus.json`，sha256 钉住，`test_di1_corpus.py` 4 项 lock 测试）；每场景五行记录见 `reports/di1-d5/evaluation.json` |
| F2 | 三指标 | **见明细** | ① semantic correctness：S1/S3/S4/S5/S6 全对（位号+连接逐条机器比对），S2 诚实拒绝（见下）；② drawing usability：质量回执客观锚点（每 committed 响应带 score/gate/blocker）+ 人工评价槽位（**PENDING_OWNER**，图纸 PNG/DXF 在 `reports/di1-d5/`）；③ deterministic replay：6/6 场景 spec_digest + outcome 双一致（双 fresh-process run） |
| F3 | 自然语言修改闭环 | **成立** | S5（生成→「换成」改图）与 S6（两句连续改图）全 committed；改图闭环的刷新断链边界已由 nl-source 端点修通 |
| F4 | 交互速度 | **冻结达标** | 方法：冻结语料逐步服务端 latency_ms（真实 TypeSafe 端点）；29 个样本（D3+D4+D5 三次测量）：median **870ms**、min 610ms、max 1972ms。**冻结阈值：median ≤ 2000ms 且 max ≤ 5000ms**（参考机型口径；样本全列不外推，不作 CI ceiling——M13 证据方法裁定） |
| F5 | 非回归 | **成立** | backend 1944 passed + ruff 净；frontend 165 单测 + 生产构建过；Playwright 71 passed / 1 skipped（无本地 TypeSafe 环境变量的干净跑）；main CI 四绿（run 38208062240，a8c413d）；M5 72-case gate 绿；语料 lock 未动 |

## 场景逐项结论

| 场景 | 结果 | 说明 |
|---|---|---|
| S1 泵罐进出料 | ✅ committed，语义全对，score 95 | 微观段 blocker 如实记录（布局引擎既有行为） |
| S2 塔顶冷凝回流 | ⚠️ 诚实拒绝（port_ambiguity） | 回流罐别名已修（F-a）；剩余拒绝=「接到 V-102」未点名端口，回执列出 in/top 两口及 selector。vessel 端口角色默认需独立设计闸门（与 G4 同族） |
| S3 泵出口压力/温度指示 | ✅ committed（D3 时子句不理解） | F-b 语法 + 泵 tap_pt；视觉上有引线穿越泵体（DRAFT_TEXT_CONNECTOR_CROSSING 已记录，布局打磨属后续） |
| S4 液位指示+控制阀回路 | ✅ committed（D3 时 no_instrument_land_port） | F-c 声明落点；LG 与罐体视觉相接处拥挤（如实记录） |
| S5 闸阀串联→换成控制阀 | ✅ 两句全 committed（D3 时「换成」拒绝） | F-e 替换语法；身份/位号/连接保留 |
| S6 进料-精馏-冷凝-回流 | ✅ 三句全 committed | 首句 score 100 且 gate passed |

## gap 清单终态

| gap | 终态 |
|---|---|
| G3 回流/侧线 selector | 已修通（D3 测定，S6 持续守护） |
| TT-101 温度变送器符号 | 已修通（D3 测定） |
| 回流罐别名置信度 | **已修通（D4 F-a）** |
| 「在 X 的出口加仪表」语法 | **已修通（D4 F-b）** |
| 双口仪表挂接语义 | **已修通（D4 F-c）** |
| 「换成」替换语法 | **已修通（D4 F-e）** |
| 系统声明子句 | 按设计 fail-closed 诚实拒绝（非缺陷） |
| G4 泄放支路走线 | **不在 DI-1 范围**——Gate 指定独立 Design Gate |
| vessel 端口角色默认（S2 暴露） | 同上，与 G4 同族待独立闸门 |

## 人工评价状态

**PENDING_OWNER**：S1/S3/S4/S5/S6 的 PNG/DXF 导出在 `reports/di1-d5/`。请 Owner 或指定工程师逐张填写 evaluation.json 中的「人工评价」槽位（是我要的图 / 不是 + 一句话理由）。在人工评价填写前，DI-1 不宣称「用户想要的图」已验证——当前证据只覆盖语义正确性、可修改闭环、速度与稳定性。

## 过程事件登记（均已在 Gate 裁定中闭合）

- D3 CI 抓到 D3 时代并发测试 flake（service 层读时序泄漏）→ 改为确定性 store 级 CAS 证明，40/40 绿；Gate 归类 test determinism issue、无产品回归。
- D4 本地抓到 m6 apply 双 apply 测试负方码集不全（漏列 semantic_baseline_moved 合法拒绝）→ 枚举补齐，40/40 绿；无产品代码受影响。
- 教训已固化：`REUSE_AND_PITFALL_LOG.md` 2026-10-12 条目（并发测试两类系统性 flake）。

## 治理状态（与 Gate 登记一致）

DI-1-D1…D4 CLOSED；D5 本片（评测+非回归+closeout）随本报告申请收口。M6-2B-D5（Minimal Drawing Qualification）为下游消费者，未启动；WS3B / M9 Closeout / M10 FINAL ACCEPTANCE 维持 Owner 挂起；DEPLOY 未授权；M6 Final Acceptance 未宣称。G4 与 vessel 端口角色默认各需独立 Design Gate。
