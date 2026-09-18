# AGENTS.md — 项目规则（P055-PID-Agent）

> 本项目位于 reasonix 工作区时继承工作区根 `AGENTS.md`（相对路径 `../../../AGENTS.md`）；发生冲突时遵循上级规则。本文件包含「总库复用与踩坑日志」一节，是 Codex 项目库的统一约定，见 `/Users/joe/ai/reasonix/NEW_PROJECT_SOP.md`。

## 项目概览

- Purpose：轻量浏览器 P&ID 软件，让工程人员与 AI Agent 共用同一套单位图例、结构化图纸和连接语义，共同创建、修改、解释和检查工艺流程图。
- Stack：Python 后端（`backend/agentcad`）+ React/TypeScript 前端（`frontend/`，Vite），Docker 部署。
- 产品显示名：P&ID-Agent；仓库 slug 规范名：p055-pid-agent；Python 导入路径暂保留 `agentcad`。

## 长期主任务书（强制读取）

- 项目长期主约束见 `PROJECT_CHARTER.md`。进行架构重构、Agent/Harness 规划、工程语义模型修改、工具体系设计、工程交付标准修改或路线图重排前，**必须先读取该文件**。
- 普通实现任务和未来模型自动重规划不得自行削弱其中的 P0 不可变核心：工程交付、semantic-first、tool-mediated editing、deterministic validation、audit/rollback、model-agnostic、人工正式审批边界。
- 更强模型可以重写可变架构和 Roadmap，但应按 Charter 的 Replanning Governance 提供迁移、benchmark、风险和回滚依据。
- 新的重大任务应尽量标明自己对应 Charter 的 milestone / priority，并说明如何使项目更接近 M9（Construction-grade Delivery Candidate）。
- 若下层产品文档、临时任务或历史实现与 Charter 冲突，默认先以 Charter 为长期方向；若确需改变核心目标，必须得到项目 Owner 明确批准并提交 Charter revision。

## 编码规则

- P&ID 的"连接正确"与"图面共线"分开验收；水平工艺链检查全局 y 唯一值，竖直支管检查全局 x 唯一值。
- 方向符号联合验收"端口 direction、connector flow_direction、图形尖端方向"三层。
- "能列模型"不等于"能生成"；模型可用性用真实最小 completion 验证。
- DELETE 等破坏性动作携带 `expected_revision`，由数据库原子比较；异步响应写状态前复核 document/revision 与请求代际。
- 关键输入不用 `window.prompt()`，使用应用内受控对话框。
- 修改后至少运行后端测试、前端构建与一次端到端画布验收。
## 总库复用与踩坑日志（双写）

本项目的可复用经验、踩坑教训、方案验证和项目复盘，按以下约定记录：

- **项目内日志**：`本项目根/REUSE_AND_PITFALL_LOG.md`——本项目踩坑明细，**执行任务前先读它**，避免重复踩坑。
- **总项目库**：`/Users/joe/ai/reasonix/REUSE_AND_PITFALL_LOG.md`——全局汇总，跨会话共享。
- 写入顺序：任务完成后 → ① 追加项目内日志（顶部，时间倒序）→ ② 同步追加总库（引用本项目 P055-PID-Agent）。
- 条目格式：`## YYYY-MM-DD · 主题（P055-PID-Agent）` + 场景/结论做法/踩坑点/适用场景。
- 触发时机：重要问题修复、踩坑排查、方案验证、项目复盘后。
- 写入失败降级：总库写不了则至少写项目内日志并注明「待同步总库」。
- 不记录：密钥、个人数据、一次性琐碎操作。
## HANDOFF 交接规则

1. **会话开始**：先读本项目根 `HANDOFF.md`，了解已完成与待办，避免重复工作。
2. **任务完成/一轮结束时**：更新本项目根 `HANDOFF.md`：
   - 「当前状态」段：重写为最新（已完成/进行中/下一步/备注），可包含关键决策与遗留问题。
   - 「近期轮次」段：顶部插入本轮记录（`YYYY-MM-DD` 标题 + 完成了什么 + 关键结论 + 下一步），**保留全部轮次不删旧**——项目级 HANDOFF 允许详细，文件增长可接受；但每条仍要清晰精炼（≤ 8 行），不复制日志。
3. **总库同步**：同时更新 `/Users/joe/ai/reasonix/HANDOFF.md`「各项目动态」中本项目的一行（覆盖式，`P0xx-名称（日期）：…`）。
4. **总库保持精简**：总库每项目仅一行，不随项目文件膨胀。
