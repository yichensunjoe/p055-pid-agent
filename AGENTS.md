# AGENTS.md — 项目规则（P055-PID-Agent）

> 本项目继承工作区根 `../../../AGENTS.md`；本文件只补充项目特有约束。

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
- 涉及后端/前端/画布运行行为的修改，至少运行后端测试、前端构建与一次端到端画布验收；纯文档检查内容、引用和 `git diff --check`。

交接与经验记录遵循工作区根规则；按任务查阅最近一级 HANDOFF 和相关日志，不默认读历史，不重复写父级/总库。
