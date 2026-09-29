# M9-WS3A 试点候选准备模板（等 Owner 填入确认）

> Gate 裁定：真实试点与专业工程师的最终选定 = Owner 动作；本模板是 Local Agent 可立即完成的 candidate-prep。优先选真实项目中的**受控 shadow/pilot 范围**，不是第一次就直接进正式施工包。
> M9 验证的是「经规定人工审核后具备进入正式流程的能力」，不要求绕过项目审批直接发施工版。

## 候选试点登记表（每个候选一份，逐项填写）

| 字段 | 内容 | 备注 |
| --- | --- | --- |
| 项目名称 / 编号 | （Owner 填） | 真实项目标识 |
| 图纸范围 | （Owner 填） | 哪几张 P&ID、新建还是修改；建议 1–3 张受控范围 |
| 专业工程师（审核人） | （Owner 填） | 姓名/资格；负责外部审查意见闭环 |
| 数据使用权限 | （Owner 填） | 工艺数据可否进入本系统/本机；脱敏要求 |
| 基准 / reference | （Owner 填） | 验收对照：既有图纸或项目约定的 deliverable 标准 |
| 预期交付状态 | （Owner 填） | 例如「经审核的 IFC 候选包」或「shadow 审查报告」 |
| 时间窗口 | （Owner 填） | 试点执行期（依赖 WS1+WS2 CLOSED 之后） |

## 验收脚本（WS3B 执行时照此跑，预写）

1. 工程师用本系统完成试点图纸的新建/修改（人工编辑 + 允许的 agent 辅助）。
2. 跑全量 validators → release-readiness 全绿（fresh）。
3. 外部审查：工程师/审图人通过 Review 面板提出意见 → 全部线程闭环（resolved + resolution_note）。
4. 申请工程批准 → operator 决策 approve（决策时 readiness eligible 绑定入 approval）。
5. 导出 evidence package（WS2 交付后）：PDF + DXF + validation 报告 + audit 链 + approval 记录单包。
6. 人工审核批准记录存档；对照基准 reference 出具差异说明。
7. 判定：满足「经规定人工审核后具备进入正式流程的能力」→ M9 Closeout 证据。

## 已知可能插入的试点前置（届时另签，不预先授权）

- G4 泄放支路绕障：若试点图纸含安全阀泄放支路且撞布局拒绝。
- LIT 双 tap：若试点要求液位计完整挂接且 DEV-4 类场景为验收项。
- HOLDOUT-1「换成」语法：若试点含设备替换类修改指令。

## 当前状态

- WS1（review workflow）：PR #67 等 Merge Gate。
- WS2（release gate + evidence package）：BLOCKED（待 WS1 合并后设计）。
- WS3B（真实试点）：BLOCKED（待 WS1+WS2 + Owner 填表确认）。
