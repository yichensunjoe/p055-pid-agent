# M9-WS3A 试点候选（Local Agent 提案 — 待 Owner 确认或改选）

> 依据 Gate 裁定：真实试点的最终选定 = Owner 动作。本表由 Local Agent 按仓库既有 synthetic-confirmed 资产拟好候选，Owner 逐项确认/修改/否决即可。数据来源全部为仓库内 synthetic-confirmed 语料（reports/m8-corpus-draft.md），无保密问题。

## 候选试点登记表（提案）

| 字段 | 内容 | 备注 |
| --- | --- | --- |
| 项目名称 / 编号 | P055-PID-Agent M9-WS3B shadow pilot（候选 A：HOLDOUT-1 完整小流程） | 真实项目标识：Owner 可替换为真实项目编号 |
| 图纸范围 | 1 张新建 P&ID：V-101→P-101→E-101（管程）→FCV-101→V-102（5 设备 4 连接，含管程 selector 一次） | 候选 A 只执行 HOLDOUT-1 句一（初次建图）；句二「换成」改图为已知前置（见下） |
| 专业工程师（审核人） | Owner（用户本人），以 operator 身份走 Review 面板 | 外部审查意见闭环由 Owner 执行；系统内 agent 仅辅助 |
| 数据使用权限 | 全部 synthetic-confirmed 语料，无真实工艺数据；零脱敏负担 | 若 Owner 改选真实项目，需补此项 |
| 基准 / reference | ① 场景期望拓扑（M8 corpus HOLDOUT-1 句一期望：多设备多连接一次 complete）；② M8-Q1 收敛记录（该路径已回归）；③ 导出 PDF/DXF 人工目检 | 差异说明对照这三条出具 |
| 预期交付状态 | shadow 审查报告 + WS2 evidence package（PDF/DXF/validation/audit-chain/approval/release 单包）；**非对外 IFC**，M9 验证「经规定人工审核后具备进入正式流程的能力」 | 符合 Gate「受控 shadow/pilot 范围」原则 |
| 时间窗口 | WS1+WS2 均已 CLOSED（main@7d5cfa3），随时可执行；预计单轮 1–2 小时含人工审查 | 等 Gate 单独放行 WS3B + Owner 确认本表 |

## 备选候选 B（若 Owner 认为 A 范围偏大）

- DEV-1 熔盐缓冲基础链（2 设备 1 连接，已验证路径回归，零 gap）：最小可闭环试点，证据链同样完整，但审查面较薄。

## 验收脚本（WS3B 执行时照此跑，预写 — 与模板一致）

1. 用本系统完成试点图纸新建（NL surface 或人工编辑，agent 辅助在现有允许范围内）。
2. 跑全量 validators → release-readiness fresh 全绿。
3. Owner 以 operator 身份通过 Review 面板提出审查意见 → **所有真实审查意见全部闭环**（resolved + resolution_note）；不人为制造 defect，无意见则如实记录「零意见」。
4. 申请工程批准 → Owner 决策 approve（决策时 readiness eligible 绑定入 approval）。
5. 执行 release 导出 evidence package；人工校验包成员与 MANIFEST。
6. 对照基准 reference 出具差异说明；批准记录存档。
7. 判定：满足「经规定人工审核后具备进入正式流程的能力」→ 回填 M9 Closeout §5/§6。

## 已知可能插入的试点前置（届时另签，不预先授权）

- **候选 A 专属**：HOLDOUT-1 句二「换成」改图（M8 deferred）——本试点只做到句一，不触发；若 Owner 要验收改图句，需先签该前置。
- G4 泄放支路绕障：本候选无安全阀支路，不触发。
- LIT 双 tap：本候选无仪表挂接，不触发（仪表审查可在 Review 线程中以意见形式覆盖）。

## 当前状态（2026-09-29 R51 更新）

- WS1（review workflow）：**CLOSED**（PR #67 → main@153c28c）。
- WS2（release gate + evidence package）：**CLOSED**（PR #68 → main@7d5cfa3，main CI 36548626197 四绿）。
- WS3B（真实试点）：**候选 A 范围已获 Gate APPROVED（shadow rehearsal READY）；REAL PILOT 差 Owner 四字段**（新会话裁定）：①真实项目锚点（受控 shadow 副本即可）②专业审核人姓名/角色/资格 ③≥1 项真实项目 reference ④数据授权登记。**Owner 补齐后自动转 EXECUTION GO**（免再审），执行前 preflight：exact SHA、无未 Review 功能变化（docs-only 可续）、环境/schema/pilot 文档 ID 固定、不扩「换成」/G4/LIT。执行纪律：干净 pilot project 从正常产品入口操作，禁 fixture/直写 DB，审查意见须真实。
- M9 Closeout 草案骨架：reports/m9-closeout-draft.md（已推 docs 分支）。
