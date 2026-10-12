# DI-1（Drawing Intelligence Iteration-1）定义提案

状态：提案（待 Gate DEFINITION 裁定）。本文档只做定义，不做设计、不写代码。

## 1. 来源（三方依据）

1. **Owner 指令（2026-10-12，原文）**：
   > 「注意你们双方一定要以快捷实用稳定高效、能真正画出来用户想要的流程图为目的进行设计优化，不要过度的工程化，影响项目进度。」
2. **Gate 方向裁定（2026-10-12，agentcad 项目权威线程 conv `6acc341a`）**：
   - 不继续堆 M6 工程化、不放弃 M6；M6 收敛为可信绘图底座；
   - M6-2B-D5 重构为 **Minimal Drawing Qualification**（5–10 张代表性样本；三指标：semantic correctness / drawing usability / deterministic replay；不要求独立第三方标注体系、大规模语料、完整 qualification framework）；
   - 建议形成新能力切片 **Drawing Intelligence Iteration-1**（暂定名）：让用户用自然语言快速生成自己想要的流程图；不新立项、不fork，属 P055 主线下一阶段；
   - WS3B / M9 Closeout / M10 FINAL ACCEPTANCE 保持 HOLD；DEPLOY 未授权。
3. **Charter**：§1 项目使命（AgentCAD = 模型负责理解/计划/修复，确定性系统负责工程对象与质量门）、§60 当前执行结论（每阶段必须用 benchmark 和真实工程案例证明更接近最终工程交付目标）。

## 2. 目标（一句话）

**用户用自然语言描述，快速得到"这就是我想要的"的流程图；不满意时用自然语言继续说，图跟着改对。**

验证的是「稳定生成用户想要的流程图」，**不是**「达到施工交付级 CAD 替代」（后者属 M9 及以后，本切片不宣称）。

## 3. 非目标（冻结）

- 不修改 M6 已冻结治理契约（八层链、唯一写入层、状态机、白名单为空、confidence≠authority）。
- 不宣称任何 qualification / 工程交付级 / 真实 P&ID 资格化完成。
- 不启动 M10、不引入新的工程审批链、不建设大规模认证系统。
- 不动 WS3B / M9 Closeout / M10 FINAL ACCEPTANCE 的 Owner 挂起；DEPLOY 仍不授权。
- 不做全行业/多标准覆盖，不做 ANSI/ISA 全量检查，不做施工级审核。

## 4. 与既有里程碑的关系

```text
P055 主线
  ├── M6 Governed Semantic Ingestion（D1–D4 CLOSED，收敛为可信绘图底座）
  │     └── M6-2B-D5（重构）：Minimal Drawing Qualification —— 作为 DI-1 的评估底座复用
  └── DI-1 Drawing Intelligence Iteration-1（本提案）：自然语言 → 用户想要的流程图
```

- DI-1 复用现有全部能力面（semantic plan/apply、deterministic drafting、validation、audit），不新造写路径。
- DI-1 的成功证据直接喂给 D5（Minimal Drawing Qualification）的三指标；两者共享样本，不重复建设。

## 5. 完成标志（草案，待 Gate 冻结）

| # | 标志 | 口径 |
|---|---|---|
| F1 | 代表性场景集 | 5–10 个真实用户场景（覆盖 Gate 裁定：简单流程线 / 设备+仪表 / 控制回路 / 管线连接 / 一个复杂局部），每个场景有输入描述、期望结果、实际生成、人工评价、replay 结果五项记录 |
| F2 | 三指标达标 | semantic correctness（元件/连接/方向正确）、drawing usability（人工评价"是我要的图"）、deterministic replay（同输入稳定重现）——逐场景如实记录，达标线随 Design Gate 冻结 |
| F3 | 交互修改闭环 | 每个场景至少一轮"用户继续说 → 图改对"的自然语言修改闭环 |
| F4 | 交互速度 | 首稿生成与修改响应在交互可接受范围（阈值随 Design Gate 按实测基线冻结，不预设硬数字） |
| F5 | 非回归 | 全量后端/前端/浏览器测试不删测不换绿；现有 M6/M13 治理行为不变 |

## 6. 证据要求（按 Owner「快捷实用」指令收窄到最小可用）

每个样本只留五行记录：输入描述 / 期望结果 / 实际生成结果 / 人工评价 / replay 结果。
**不要**：独立第三方标注体系、大规模 gold corpus、完整 qualification framework。

## 7. Charter 修订请求（MINOR，依据 §59 新增里程碑）

随本提案请求 Charter 1.2.0 → 1.3.0 MINOR revision：新增 §55D（DI-1），Revision Proposal Record 六段草案：

- **Immutable Core**：不变（工程交付、semantic-first、tool-mediated editing、deterministic validation、audit/rollback、model-agnostic、人工正式审批边界全部保留）。
- **Mutable Architecture**：允许新增自然语言出图/改图的能力迭代切片及其轻量评估证据形态。
- **Roadmap**：§55D 插入 M13 之后；M9/M10 完成标准不变；M6-2B-D5 重构口径写入 §51 注释。
- **Benchmark**：禁弱化既有回归；DI-1 证据形态为新增，不替换既有门禁。
- **Migration**：无 schema/迁移内容（定义阶段）。
- **Owner approval**：本提案即 Owner replanning trigger（2026-10-12 指令）的落实。

## 8. 切片顺序建议（待 Gate 裁）

- **DI-1-D1**：定义裁定（本文档）+ Charter MINOR revision（docs-only）。
- **DI-1-D2**：设计与 CODE GO 申请（场景集冻结、评价口径、证据格式、达标线）。
- 后续切片由 Gate 逐片授权，不一次授权。

## 9. 当前不做的事

D5（重构后）的正式开启、任何生产部署、任何对 M6 冻结契约的修改、任何"资格化已完成"的对外表述。
