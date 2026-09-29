# M9 — Construction-grade Delivery Candidate · 差距分析与立项 proposal（报 Gate 签 M9 Design Gate）

> 日期：2026-09-29 ｜ 基线：main=`fbcadae`（M8 milestone CLOSED，#56–#66 十一 squash）。
> 依据：PROJECT_CHARTER §54（M9 完成标志）、§56（重大规划强制七问）。已按强制清单重读 AGENTS.md / PROJECT_CHARTER.md / HANDOFF.md / docs/product-vision.md / docs/architecture.md / 现状测试与 REUSE_AND_PITFALL_LOG。

## 一、M9 退出条件逐项对照（现状盘点）

| # | M9 完成标志 | 现状 | 差距 |
| --- | --- | --- | --- |
| 1 | 真实试点项目中由专业工程师完成完整 P&ID 新建或修改 | 全链（NL/手工→语义→布局→物化→导出）已通；无试点项目与真人流程 | **流程缺口**：需选定试点项目+专业工程师参与；非纯代码 |
| 2 | 系统 validators 全部通过 | api_validation 面 + release-readiness（assess_release_readiness + readiness hash + audit 事件）已存在 | **门禁缺口**：validator 结果未与 release state 强制绑定（未全绿可否标 release？当前无强制） |
| 3 | 外部审查意见可闭环 | **无任何 comment/review 实体与路由**（全库无 comment 模型）；approval 只有 ToolApproval 骨架（agent apply 需显式 approval_id） | **最大代码缺口**：审查意见（锚定元素/revision 的 comment 线程）+ 人审 approval 工作流（request→approve/reject + 身份 + 与 readiness 绑定）完全缺失。此亦章程 M8 原始完成标志 |
| 4 | PDF/DXF/项目包满足项目约定 | PDF/DXF 导出全通（含实体 DXF readback 证据） | **项目包缺口**：evidence package 正式导出（图 + validators 结果 + audit 链 + approval 记录的单包 artifact）缺 |
| 5 | revision、audit、approval 完整 | revision/undo/audit/provenance 完整；approval 半成 | 同 #3 的人审工作流 |
| 6 | 人工审核批准后进入施工交付流程 | 无 release state 机（只有 readiness 评估） | 依赖 #2/#3：release 状态与审批绑定的状态机缺 |

## 二、章程 §56 七问回答

1. **距 M9 最大瓶颈**：审查闭环（comment + 人审 approval + release 状态机三者组成的 review workflow）——代码完全缺失且是 M9 六个条件中三个条件的前置。
2. **本任务属哪个 milestone**：M9（Construction-grade Delivery Candidate）。
3. **是否强化/削弱 P0**：强化 P0-6（可回滚、可审计、可审批——把审批从 agent 工具级升格为工程级人审）；不动 P0 其余。
4. **如何验证**：每 workstream 走既有 Gate 纪律（设计→签→实现→单测+全量门禁+e2e→PR→exact-head CI→Merge Gate）；M9 验收另需一次真实试点演练（真人真项目）。
5. **是否需 migration**：新增 comment/approval/release 数据面，走 store 迁移契约（既有 migration 机制）；存量文档默认无 comment/approval，向后兼容。
6. **是否影响 engineering data**：comment/approval 是图纸的治理数据，须进 audit 链与 spec 身份隔离（comment 不进 layout digest——它是审查意见不是工程事实）。
7. **release risk**：低-中；全部新增面，不动既有 frozen 链；最大风险是试点流程依赖人（需 Owner 指定试点项目与工程师）。

## 三、提议的 M9 工程范围（三个 workstream，请裁定切分与顺序）

- **WS1 Review Workflow（核心）**：comment 实体（document+revision+element 锚定、线程、resolve 状态）+ 人审 approval 工作流（request→approve/reject、操作者身份、audit 事件、与 readiness 绑定）。补上章程 M8 原始标志。
- **WS2 Release Gate + Evidence Package**：validator 全绿 + approval 通过 → 才可置 release state（状态机 + 冻结语义）；evidence package 单包导出（PDF/DXF + validation 报告 + audit 链 + approval 记录 + provenance）。
- **WS3 试点准备（流程）**：试点项目/工程师选定、真实工艺输入、外部审查演练计划；按试点需要决定是否插入 G4/LIT 双 tap 作为试点前置（届时另签）。

## 四、请 Gate 裁定

1. M9 范围按 WS1/WS2/WS3 切分是否批准，顺序如何（建议 WS1→WS2→WS3 并行准备）；
2. WS1 设计的冻结边界（comment 是否进 semantic spec 还是纯治理面——建议纯治理面、不进 digest）；
3. 试点项目的选定是否属 Owner 侧动作，还是需要我先出候选方案；
4. 签署后我从 WS1 设计开始按老纪律推进。
