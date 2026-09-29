# M10 Design Draft v1 — AgentCAD Platform（Harness / P&ID 解耦）

> Gate：M10 DESIGN PREP GO 已批（2026-09-29，新会话 conv 6abbdafb）。本文档为设计阶段产物，**不含任何代码变更**；M10 CODE IMPLEMENTATION 未获授权。
> 基线：main@7d5cfa33b7faee4656efffff636fcdd83a68b83c。M9 状态并行不变：WS1/WS2 CLOSED，WS3B BLOCKED ON OWNER，M9 Closeout PENDING。
> M10 完成标志（Charter §55 原文）：Harness 与 P&ID domain 解耦，第二个 engineering drawing domain 能复用 runtime，且 P&ID 能力不退化。Charter 原前置「P&ID 达到稳定工程使用」本次由 Owner 显式重排（replanning trigger），替代成熟度门的处理见 §8。

## 1. 现状耦合盘点（coupling inventory）

对 main@7d5cfa3 实际源码的逐 import 核验，耦合分三类：

**A. Harness 核心（已领域无关，是 runtime 的直接候选）** — `harness.py` 的 session 生命周期（create/transit/ensure/complete）、approval 请求与裁决（request_approval/resolve_approval）、`authorize()` 全链（permission 策略 deny/ask/allow、canonical intent、intent_hash 绑定、approval 与 session/tool/document/intent 四元组核对、拒绝即留证）、tool-call 记录、audit 事件发射（`audit.request_audit_context` + 通用 event_type）。这些代码只依赖 **tool_name / intent(Any) / document_id / revision**，不含任何 P&ID 概念。

**B. Harness 边缘的 P&ID 硬耦合（要切开的接缝）** — `harness.py` 顶部 import 的 5 个 P&ID 请求模型：`models.TransactionRequest/TransactionResult`（`apply_authorized` 直调 `service.apply_transaction`）、`agent_semantic_models.SemanticTransaction`、`drafting_models.DraftingRequest`、`layout_models.AutoLayoutRequest`、`m7_synthesis_models.SynthesisProposalEvidence`（两个 canonicalize 特例与 synthesis evidence 存取）；外加 `service.DocumentService` 与 `tool_registry.get_default_tool_registry`（P&ID 工具定义）。**执行语义（谁来跑被批准的工具）当前焊死在 P&ID 工程写上**。

**C. 天然通用但随包分发的底座** — `audit.py/audit_hash.py/audit_models.py`（全局 hash chain，领域无关）、`store.py`（SQLite 单库多面：documents/review_state/audit/tool_calls/sessions/approvals；其中 tool_calls/sessions/approvals 三面已是通用治理表）、`diagnostics.py`、`harness_models.py`（仅依赖 `models.StrictModel`）、`surface_contract.py`（声明式合同，P&ID 路由占绝大多数但机制通用）。

**结论**：M10 的真实工作量不在「写新平台」，而在 **B 类接缝的协议化** 与 **包边界物理化**；A/C 类证明 runtime 核心已成熟，解耦不是 premature abstraction 的赌博——耦合是历史打包方式造成的，不是设计缺陷。

## 2. 目标边界（target Harness/domain boundary）

引入唯一的接缝抽象——**工具执行器协议**：

```
class ToolExecutor(Protocol):            # agentcad/runtime/
    def execute(self, *, authorized, intent) -> ExecutionResult
    # authorized = 已核定的工具调用（session/tool/intent_hash/approval 绑定齐全）
    # intent 为领域自己的请求模型；runtime 不理解其内容，只绑定其 hash
```

- `agentcad/runtime/`（新包，**import 禁令：不得 import 任何 P&ID 模块**——models/service/tool_registry/validation/review/release/layout/drafting/semantic/m7_* 全部禁止）：harness 核心 + harness_models + executor 协议 + session/approval/tool_call 的 store 访问薄层。
- `agentcad` 顶层保留 P&ID domain（现有全部模块原位不动）；新增 `PidToolExecutor` 适配器：把 `apply_authorized` 现有逻辑（P&ID 五个请求模型的反序列化 + `service.apply_transaction` + semantic diff 绑定）整体收进适配器，harness 核心改为委托 executor。
- `canonicalize_tool_intent` 的两个 P&ID 工具特例随适配器下沉；核心只保留通用 canonicalization（JSON 规范化排序）。
- 治理面（review/release，M9 交付）**不进入 runtime v1**：它骑在 store + service 上且已领域无关化良好，第二轮再议；v1 范围严守「Harness 解耦」这一 Charter 完成标志的字面要求。

## 3. 第二 domain 证明切片（proof slice，仅为复用证明）

Gate 冻结：第二 domain 只是复用证明对象，不得长成新产品。设计为 **test-only 的最小 schematic domain**：

- 领域 B = 「线缆示意图」最小定义：一个文档类型 + 一个工具 `add_cable_segment`（permission=ask，engineering_write 类），请求模型 CableSegmentRequest（id/from/to/gauge）。
- 它提供：自己的 CableExecutor（实现 ToolExecutor）、自己的 CableToolRegistry 条目（复用 ToolDefinition/ToolRegistry 结构）、自己的 CableService（极小文档面，写自己的表或复用 documents 表 + type 字段——见 §7 数据兼容）。
- **验收证明**：同一段 runtime 代码路径完成 session → authorize(ask) → approval 绑定 → executor 执行 → audit 落链，全程 runtime 包零 P&ID import；P&ID 侧同时跑一条等价路径证明双 domain 共存。
- 边界纪律：不建 UI、不建导出、不建 validator profile、不进 MCP 公开面；测试用例即其全部存在形式。

## 4. 迁移 / 抽取时序（extraction sequencing，实施阶段执行，本阶段不动代码）

- **P1 搬不可变的**：harness_models.py → runtime/（顶层 re-export shim 保 import 路径兼容）；纯函数（canonicalize 通用部分、intent_hash）随核心搬迁。**零行为变化**。
- **P2 切执行接缝**：引入 ToolExecutor 协议；`apply_authorized` 改造为委托；PidToolExecutor 收编现有 P&ID 逻辑；canonicalize 特例下沉。**行为不变，接缝出现**。
- **P3 物理边界 + 非回归硬锁**：AST import-lint 测试（runtime/*.py 不得出现 P&ID 模块 import）；子进程隔离测试（不装 P&ID 侧模块即可 import runtime 并完成一次内存态 approve 流）；既有 1692 测试全绿 + M5 deterministic + e2e。
- **P4 证明切片**：§3 的线缆 domain 测试落地（test-only）。
- 每步独立小 PR，可独立回退；P1–P2 之间任意点回退 = git revert。

## 5. P&ID 兼容性策略（不退化是完成标志的一半）

- 公开面零变化：HTTP 路由、MCP 工具名、请求/响应 schema、audit 事件类型、WS1/WS2 治理语义全部原样。
- shim 层保内部 import 路径（旧 import 继续工作至少一个 milestone，下一 milestone 再清理）。
- 非回归门（每 PR 必过）：backend 全量测试、M5 72-case deterministic、Chromium e2e、ruff、surface contract 机器锁、audit chain 结构测试。
- 性能基线：抽取只移动代码与增加一次协议间接层，benchmark = 全量测试墙钟时间前后对比（接受阈值 ≤ +5%）。

## 6. 数据兼容性

- **零 schema 变更**：v13 不动；sessions/approvals/tool_calls 表结构不动；audit chain 不动。
- 领域 B 若需要 CableDocument 存储，优先复用 documents 表 + `type` 区分（现有 documents 是否已有 type/project 字段实施时核验），不建新库；若必须加表，additive 迁移 v14，同 M9 的 v13 纪律（无 FK 级联、旧库零记录直读）。

## 7. 回滚

每个 PR 独立 revert 即回滚到上一稳定态；P2 是唯一引入新抽象的 PR，其回滚不触碰 P1 的搬迁成果。无数据迁移意味着无回滚数据风险。

## 8. 成熟度门处理（请 Gate 二选一冻结）

Charter 原前置「P&ID 达到稳定工程使用」被 Owner 重排。两种合法替代：

- **选项甲（推荐）**：WS3B 真实试点 + M9 Closeout 保持为 **M10 最终签收的完成前置**，但 **P1–P4 代码切片的 CODE GO 不等待它**——即 M10 实施先行、签收押后。理由：Charter 明确反对 P&ID 未成熟时平台化；把「签收」绑在真实使用证据上，既尊重 Immutable Core 的精神，又让解耦工程不被 Owner 的试点排期卡死。
- **选项乙**：Owner 接受一个替代成熟度门（例如：shadow rehearsal PASS + 连续 N 个 milestone 全量回归零回滚 + review/release 治理面在真实人工审查中的使用记录），M10 签收不依赖 WS3B。

**不建议**第三种（完全取消成熟度门）—— Charter 的文字虽然把路线列为 mutable，但其理由（防止 premature abstraction）在 §1 的耦合盘点中已被证据削弱，仍值得保留一道门。

## 9. 工程风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| import 环（runtime↔pid 双向依赖） | 中 | import-lint 硬测 + P1 先搬叶子模块 |
| 协议层抽象泄漏（intent 类型回渗 runtime） | 中 | intent 在 runtime 一律 Any + hash 绑定；类型化只存在于各 domain 适配器 |
| 测试 churn（1692 测试的 import 路径） | 低 | shim 保路径 + P1 纯搬运 |
| 第二 domain 范围蠕变 | 低 | Gate 已冻结 test-only；P4 PR 描述逐条对 §3 边界 |
| harness 行为漂移 | 低 | P2 改造前后 `test_harness*` 全量不动即锁 |

## 10. 实施 PR 切片摘要（供 CODE GO 逐片授权）

| PR | 内容 | 行为变化 | 回滚 |
|---|---|---|---|
| M10-P1 | harness_models + 核心搬迁 runtime/ + shim | 零 | revert |
| M10-P2 | ToolExecutor 协议 + PidToolExecutor + 委托化 | 零（接缝出现） | revert |
| M10-P3 | import-lint + 隔离测试 + 全量非回归 | 零 | revert |
| M10-P4 | 线缆 domain 证明切片（test-only） | 零（测试资产） | revert |

## 11. 本文档不含的东西（边界自锁）

不改 runtime 行为、不改 schema、不改公开 API/MCP、不做真实代码抽取（本阶段）、不启动第二 domain 产品化、不动 WS1/WS2/WS3 任何语义、不写 M9 为 closed。
