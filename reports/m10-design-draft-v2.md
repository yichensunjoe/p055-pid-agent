# M10 Design Draft v2 — R10-1~R10-5 闭合（delta 文档，基座为 v1）

> 依据 Gate 对 v1 的 CHANGES REQUIRED（R10-1~R10-5）+ 成熟度门冻结「甲」+ PR 顺序裁决（P1/P2 分开）。本文档只写 delta 与闭合矩阵，v1 其余仍然有效。docs-only，零代码。

## R10-1 闭合：「唯一抽象 ToolExecutor」废弃，改为 runtime 端口组（DomainAdapter 协议集）

v1「唯一新抽象 = ToolExecutor」不成立——`harness.py` 实测还有 6 处 domain 耦合（已逐行核验 main@7d5cfa3）：`create_session`→`service.get_document()`；`preview_intent_diff_hash`→构造 TransactionRequest + semantic diff；`canonicalize_tool_intent`→理解 SemanticTransaction/AutoLayoutRequest/DraftingRequest；synthesis proposal evidence（P&ID 专有状态）；`_approval_evidence`→理解 transaction.operations/element_id；`_record_rejected_call`→直调 `service.audit.record_rejection()`。

v2 冻结 runtime 面向 domain 的端口组（`agentcad/runtime/ports.py`，全部 Protocol）：

```python
class DomainAdapter(Protocol):
    # ① 文档上下文（create_session 用：存在性 + 当前 revision）
    def document_context(self, *, document_id: str) -> DocumentContext  # {document_id, revision}
    # ② 意图规范化（authorize 链用；runtime 内不再留任何 P&ID 特例）
    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any
    # ③ diff preview（approval 请求用；best-effort 永不抛）
    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str
    # ④ approval evidence（request_approval 用）
    def approval_evidence(self, *, definition, canonical_intent, diff_preview_hash) -> dict
    # ⑤ rejection evidence（authorize 拒绝路径用）
    def rejection_evidence(self, *, definition, intent, error_code) -> dict
    # ⑥ 执行 + ⑦ 原子 provenance 闭包（R10-4 见下）
    def execute(self, *, authorized, audit_context, closure, intent) -> ExecutionOutcome
    def closure_request(self, *, authorized, intent) -> ClosureRequest
```

- runtime 保留：session 生命周期、authorize()、intent_hash 链、tool-call 记录、事件发射——全部经端口（见 R10-3）。
- `PidDomainAdapter` 收编全部 7 个端口的 P&ID 实现（TransactionRequest 反序列化、semantic diff、operations 摘要、apply_transaction 调用）。
- **synthesis proposal evidence 按 Gate 建议留在 P&ID facade**：`record_synthesis_proposal_evidence/latest_*` 从 harness 下沉为 P&ID 侧 store 辅助函数（api 层直调 store），runtime v1 不出现该概念。

## R10-2 闭合：中性原语层（StrictModel / utc_now 抽离）

冲突确认：`harness_models.py` 依赖 `models.py` 的 StrictModel/utc_now，而 models.py 是 P&ID Document/Element 入口，「纯搬运 + runtime 禁 import models」互相矛盾。

v2 冻结：新建 `agentcad/runtime/primitives.py` 作为 **StrictModel 与 utc_now 的唯一定义处**；`models.py` 改为 `from .runtime.primitives import StrictModel, utc_now` 并 re-export（既有 `from .models import StrictModel` 的几十个调用方零改动）；`harness_models.py` 改从 runtime.primitives 导入。**同一类对象 → JSON schema/校验行为/序列化逐字节不变**。P1 的第一步就是这个搬迁。

## R10-3 闭合：runtime 不得直接依赖 store.py / audit.py（传递依赖封锁）

Gate 核出的传递链成立：`store.py` 顶部 import Document/M6/M7/project settings/review state；`audit.py` import Document/TransactionRequest/semantic_diff。v2 冻结两个端口：

```python
class HarnessStorePort(Protocol):      # session/approval/tool-call 三面，仅此
    def get_agent_session / create_agent_session / save_agent_session(...)
    def get_tool_approval / create_tool_approval / save_tool_approval(...)   # 以现有方法名为准
    def get_tool_call / create_tool_call / save_tool_call(...)
class AuditPort(Protocol):             # 中性事件记录，不含 diff/validation 语义
    def record(self, *, event_type, actor, surface, status, error_code="",
               evidence, session_id=None, approval_id=None, tool_call_id=None,
               document_id=None, base_revision=None) -> AuditRecordRef
```

- `SQLiteDocumentStore` 与 `AuditRecorder` 作为**实现方**继续存在并被注入；runtime 只持有端口句柄，源码层面不知其类。
- **P3 传递边界锁升级为子进程隔离测试**（不靠 AST 扫字面 import，防 `runtime→neutral-looking→store.py→models.py` 绕行）：`python -c "import agentcad.runtime.harness, sys; bad=[m for m in sys.modules if m.startswith('agentcad') and not m.startswith('agentcad.runtime')]; assert not bad"`，进程非零退出即红。外加 import 后行为烟测（内存 fake store/adapter 走通一次 approve 流）。

## R10-4 闭合：原子 provenance 闭包写进执行契约 + P2 硬测

现状（已核验）：`apply_authorized` 把 `ProvenanceState(tool_call_id, consume_approval, close_session)` 交给 `service.apply_transaction`，使 **工程 revision + audit record + tool-call 闭包 + approval 消耗 + session 完结落同一 SQLite transaction**；失败路径 `record_failure` 也在单事务内留证。

v2 冻结契约 postcondition（写进 ports.py 文档串与 P2 测试名）：
- 成功：五闭包齐现（revision 前进、audit applied 一条、tool_call completed、approval consumed、session completed）；
- 失败：**不允许**「图已改但 approval/tool-call/session 未闭合」，也**不允许**反向半状态；失败证据恰一条。
- 机制：原子性留在 domain 的 governed write 路径内部（PidDomainAdapter.execute 如现状调用 apply_transaction(audit=…, state=…)）；runtime 把 AuditContext + ClosureRequest 传入并要求适配器保证 postcondition（接口注释冻结，违规即实现 bug）。
- **P2 硬测**（Gate 点名至少一条）：注入 mid-commit 失败 executor → 断言 revision 不变、无悬空 started tool_call、approval 未消耗、session 未闭合、恰一条 failure audit；另加成功路径五闭包断言。

## R10-5 闭合：v1 全程 schema v13 冻结，P4 禁用 v14

矛盾确认：v1「零 schema」与 P4「documents+type 或 v14」未冻结。按 Gate 收紧：

- **P1–P4 全程 schema v13 不动，禁止 v14**；documents 表不加 type 列。
- Cable proof 用**内存态 CableRepository**（dict-backed，挂在 test 内）；persistence 不在证明范围（证明对象是 runtime 复用，不是第二 domain 的库设计）。Cable 侧也提供 HarnessStorePort 的内存 fake 完成 session/approval/tool-call 闭环。
- 第二 production domain 的持久化设计明确**不属于**本批 M10。

## 成熟度门（已获冻结：甲，复述锁定）

WS3B REAL PILOT + M9 Closeout = **M10 FINAL ACCEPTANCE 前置**，不是 P1–P4 实施前置。设计→P1→P2→P3→P4→技术完成→（若 M9 未收口则等待）→WS3B PASS + M9 Closeout→M10 FINAL ACCEPTANCE。乙被否（shadow rehearsal 不能稀释 real-pilot 证据口径）。

## PR 顺序（按 Gate 裁决修订）

| PR | 内容 | 行为变化 |
|---|---|---|
| M10-P1（单独） | runtime/primitives.py（StrictModel/utc_now 唯一定义 + models.py re-export）+ runtime/ports.py + 兼容 shim；**禁 harness 行为重构** | 零 |
| M10-P2（单独） | harness 切 DomainAdapter 端口组 + PidDomainAdapter + R10-4 原子闭包硬测 | 零（接缝出现） |
| M10-P3 | R10-3 子进程隔离锁 + fake-store 烟测 + 全量回归 | 零 |
| M10-P4 | schema-free 内存态 cable proof（test-only） | 零（测试资产） |

## 闭合矩阵

| 项 | 处置 | 落点 |
|---|---|---|
| R10-1 | DomainAdapter 七端口；synthesis 沉 P&ID facade | §R10-1 |
| R10-2 | runtime/primitives.py 中性原语 + re-export | §R10-2 |
| R10-3 | HarnessStorePort/AuditPort + 子进程传递锁 | §R10-3 |
| R10-4 | 五闭包 postcondition + P2 双向硬测 | §R10-4 |
| R10-5 | v13 全程冻结、P4 内存态、禁 v14 | §R10-5 |
| 成熟度门 | 甲（复述锁定） | §成熟度门 |
| PR 顺序 | P1/P2 分开 | §PR 顺序 |
