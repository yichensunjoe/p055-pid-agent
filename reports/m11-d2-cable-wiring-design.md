# M11-D2 详细设计 v2：Cable Runtime Production Wiring & Atomic Governed Write（送 Gate 审，docs-only）

> 前置：M11-D1 CLOSED（main@5ea00b3，identity-only registry + v14 frozen envelope）；D2 DESIGN PREP GO 已签（A1~A3 修正已并入）。基线 main@5ea00b3ae5b2729c8b99a21c41e2b9b424f8ce82。核心问题：在 schema v14 不变、registry identity-only、Cable revision 单真相条件下，Cable 经七端口 DomainAdapter 进入 M10 runtime，使 **Cable payload mutation + cable_documents.revision + 三项治理闭包 + 全局 audit append 成为一个 SQLite 原子提交**。

## 1. Cable 持久化单一真相（A1/A2 冻结）

- `cable_documents.revision` = Cable **唯一 authoritative revision**；`documents_registry` 无 revision 列（identity-only，D1 已冻结）；**不存在「registry revision」概念**（A1）。
- `cable_documents.data_json` = CableDocument 语义载荷（segments 等）；**data_json 内不含 authoritative revision**（A2）。反序列化时由 envelope revision 注入 CableDocument；若 JSON 中保留 revision 缓存字段，读写强校验与 envelope 相等，不等即 fail-closed。
- 三段真相层级：identity（registry）/ revision+payload（cable_documents envelope）/ 治理（三表+audit）——各管一件事。

## 2. CableDocument JSON contract（data_json，冻结）

```json
{
  "schema": "pid-agent.cable-document/1",
  "name": "...",            
  "segments": [
    {"id": "CBL-001", "from_node": "MCC-1", "to_node": "PMP-101", "gauge": "4mm2"}
  ]
}
```
- `schema` 字段是版本锚（pydantic 校验，未知版本拒读）。
- 领域 invariant（承接 P4 冻结并升入生产语义）：`from_node != to_node`；segment id 在文档内唯一；段 id 全局唯一性不做跨文档保证（文档内作用域，同 P&ID element_id 语义）。
- 增删 segment = 文档级 mutation，产生 revision 前进（与 P&ID 事务同级语义）。
- **不创建 cable_segments 表、不升 v15**（A3）：若实现中证明 normalized 表是生产必需 → STOP / RETURN TO GATE。

## 2A. AddCableSegmentIntent（R11-D2-1 冻结——expected_revision 进入审批绑定）

```
AddCableSegmentIntent
  expected_revision: int >= 0        # 审批绑定的一部分，canonical intent 包含它
  segment: {id, from_node, to_node, gauge}
```

冻结链：canonical intent 含 expected_revision → approval evidence 明示它 → preview_diff_hash 基于该 revision → composition 调 `authorize(..., base_revision=intent.expected_revision)` → execute() 硬断言 `authorized.record.base_revision == intent.expected_revision` → store CAS 用同一值。五处同值，堵住「审批看旧状态、执行换 base」的绑定缺口（对齐 P&ID TransactionRequest.expected_revision 先例）。

## 3. CableDomainAdapter 七端口（生产实现，不经 PidDomainAdapter）

| 端口 | 生产语义 |
|---|---|
| document_context | 读 cable_documents envelope → DocumentContext(document_id, revision=envelope.revision)；行不存在 → DocumentNotFound 语义 |
| canonicalize_intent | 校验 AddCableSegmentIntent（含 expected_revision）并填默认值后 model_dump；from==to 即拒（invariant 前置） |
| preview_diff_hash | CableDocument 差异确定性 hash（canonical JSON sha256；best-effort 不抛） |
| approval_evidence | {tool, cable_document_id, cable_segment_id(s), diff_preview_hash} |
| rejection_evidence | {tool_permission, cable_document_id, authorized: false} |
| closure_request | ClosureRequest(tool_call_id, consume_approval, close_session=True, metadata={cable_document_id, applied_segment_count}) |
| execute | 调 store.commit_cable_write（§4），返回 ExecutionOutcome(payload=CableWriteResult) |

## 4. store.commit_cable_write 原子原语（D2 核心）

单一 `BEGIN IMMEDIATE` 事务内完成（模式同 M9 commit_release / D1 边界）：

```
1. 复核 cable_documents 行存在且 revision == expected_cable_revision（CAS，防跨平面竞态）
2. 应用 mutation（增删 segment，重跑文档内 invariant）
3. UPDATE cable_documents SET revision=+1, data_json, updated_at
4. tool-call 闭包（completed/result_revision）+ approval 消耗 + session 完结
5. audit append（event_type="revision.created"，status="applied"，evidence 含 cable_document_id + segment 摘要）
6. COMMIT；任一步失败整体 ROLLBACK
```

## 5. 失败语义（R11-D2-3 重写：success-tx 回滚 → failure-closeout tx，两级 audit 失败如实）

**结构**：success primitive（§4 六步）任一失败 → 该事务完整回滚（Cable 载荷/envelope revision 绝不变）→ 进入 **failure-closeout 事务**（独立于 success tx）：tool-call → failed（error_code 精确）、session → failed、approval 保持 approved 不 consumed、恰一条 revision.created status=rejected 且绑 tool_call/session/approval 三关联（R72-3A 语义）。「恰一条 rejected evidence」是 **failure-closeout 事务成功**的 postcondition。

**一级**：success 失败但 failure-closeout 可持久化 → 上述 postcondition 全成立，异常上抛原错误。

**二级**：failure-closeout 自身也无法持久化 → 两事务全部回滚，原始/二级 persistence error 显式上抛，**不得伪报 evidence 已记录**；此时允许 tool-call 仍 running、session 仍 active、approval 仍 approved、Cable 载荷/revision 零变化。

**硬测两条**：① success audit append 一次性失败、failure-closeout audit 正常 → 恰一条 rejected；② success audit 与 failure audit 持续失败 → Cable 零变化、无半闭包、异常显式上抛。

禁止「先提交 Cable 再补 audit」——success 路径永远单事务。

## 5A. Cable 文档 bootstrap 创建（R11-D2-4 冻结）

`CableService.create_document()` = **provisioning/bootstrap 生命周期操作，不是 engineering mutation**——空文档须先于任何 session 存在（runtime create_session 先调 document_context），故它天然不进 runtime 审批流。但它必须可审计，冻结为单事务：INSERT documents_registry(domain='cable') + INSERT cable_documents(revision=0, data_json=空文档) + audit append（event_type="document.created"，status="applied"，evidence 含 cable_document_id）→ COMMIT；任一失败整体回滚。**此后任何 segment engineering mutation 一律经 M10 runtime**（session→approval→execute）。与 P&ID create-document 的可审计习惯一致，不伪装 bootstrap 能过 runtime。

## 6. Global audit chain（硬锁）

复用现有全局 chain：audit_records schema / global ordinal / hash formation / chain schema/version / genesis verification 全不变；Cable 与 P&ID audit 交错可验（verify_chain 从 genesis 全链验证天然覆盖）。若发现必须改任一 → STOP / RETURN TO GATE。

## 7. ToolRegistry production boundary

Cable 用 **cable-specific 独立 registry 实现 ToolRegistryPort**（不复用 P&ID catalogue、不扩它）：`CableToolRegistry` 冻结一个工具 `add_cable_segment`（permission="ask"，risk="engineering_change"，audit_event="tool.cable.add_segment"，description 冻结）。禁止把 P&ID catalogue 变成隐式跨域 catalogue；runtime 只见中性 ToolDefinitionView。

## 7A. CableAuditAdapter : AuditPort（R11-D2-2 冻结）

runtime 在 execute 之前的拒绝路径（approval 缺失/已拒/四元组不匹配/permission deny）由 runtime 自建 rejected ToolCall 并直调 `audit_port.record(AuditEvent(permission.rejected))`——这些事件**不进** §4 原子写。故 D2 提供 `CableAuditAdapter`（置于 cable_domain_adapter.py）：与 PidAuditAdapter 同构——中性 AuditEvent 逐字段转 `request_audit_context` + `AuditRecorder.record_rejection`，hash 形成/链语义全在 audit 实现内。composition 必须注入它；测试覆盖 Cable 拒绝路径留证（含 approval mismatch）。

## 8. Composition scope

test composition root + internal CLI/dev composition root（ CableService(store) + CableDomainAdapter + CableToolRegistry + AgentHarnessRuntime 手工组装，同 P3/P4 探针的组装方式但接真 store）。不进公开 HTTP/MCP/REST（D3/D4 或后续切片）。

## 9. 双 domain 隔离硬测（冻结 ≥8 条）

① Cable write 不改任何 P&ID document/revision/digest；② P&ID write 不改 cable envelope；③ cable id 走 P&ID loader → fail-closed；④ P&ID id 走 Cable loader → fail-closed；⑤ 全局 audit ordinal 可 P→C→P→C 交错连续；⑥ 两 domain 同时有 session/approval/tool-call 各行其道；⑦ cross-domain approval/intent/document 绑定不可串用（拿 P&ID approval 批 Cable intent → ToolIntentMismatchError）；⑧ 100 轮交错操作属性测试零污染。

## 10. D2 禁止项（本阶段与 CODE GO 均禁）

validator profile / export / UI / public REST/MCP / P&ID production behavior 修改 / runtime ports|models 修改 / audit hash-chain 修改 / schema v15 / cable_segments 生产表。

## 11. 待裁决策点

1. §2 JSON contract 与 invariant 集合；2. §4 原语六步顺序与失败矩阵口径；3. §7 cable-specific registry 方案；4. D2 CODE GO whitelist 预告：新增 cable_service.py / cable_models.py / cable_domain_adapter.py（生产）+ store.commit_cable_write + cable registry + tests/test_m11_d2_cable_runtime.py + 双隔离硬测；不动 runtime 包、不动 P&ID 模块、不动 schema。

请裁 M11-D2 Design；PASS 后请签 M11-D2 CODE GO。
