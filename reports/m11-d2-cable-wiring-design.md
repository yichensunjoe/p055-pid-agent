# M11-D2 详细设计：Cable Runtime Production Wiring & Atomic Governed Write（送 Gate 审，docs-only）

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

## 3. CableDomainAdapter 七端口（生产实现，不经 PidDomainAdapter）

| 端口 | 生产语义 |
|---|---|
| document_context | 读 cable_documents envelope → DocumentContext(document_id, revision=envelope.revision)；行不存在 → DocumentNotFound 语义 |
| canonicalize_intent | 校验 `add_cable_segment` intent 为 CableSegmentRequest 并填默认值后 model_dump；from==to 即拒（invariant 前置） |
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

## 5. 失败矩阵（每类：不变量 / tool-call 终态 / session / approval / 恰一条失败证据）

| 失败类 | 必须不变 | tool-call | session | approval | 失败证据 |
|---|---|---|---|---|---|
| storage failure（2/3 步：CAS 不过、invariant 违例） | envelope revision 与 data_json 原样；无 audit 变化 | failed（error_code 精确） | failed | 仍 approved 不 consumed | 恰一条 revision.created status=rejected，绑三关联（R72-3A 语义对齐） |
| closure write failure（4 步） | 同 storage | failed | failed | 仍 approved | 恰一条 rejected（同事务回滚后由 record_failure 路径重放——record_failure 自带单事务留证） |
| audit append failure（5 步） | 载荷与闭包随事务回滚（不留半截） | failed | failed | 仍 approved | 恰一条 rejected（失败路径的 audit 写入若本身再失败 → 上抛且全回滚，不留任何痕迹，重试安全） |

禁止「先提交 Cable 再补 audit」——所有路径同事务。

## 6. Global audit chain（硬锁）

复用现有全局 chain：audit_records schema / global ordinal / hash formation / chain schema/version / genesis verification 全不变；Cable 与 P&ID audit 交错可验（verify_chain 从 genesis 全链验证天然覆盖）。若发现必须改任一 → STOP / RETURN TO GATE。

## 7. ToolRegistry production boundary

Cable 用 **cable-specific 独立 registry 实现 ToolRegistryPort**（不复用 P&ID catalogue、不扩它）：`CableToolRegistry` 冻结一个工具 `add_cable_segment`（permission="ask"，risk="engineering_change"，audit_event="tool.cable.add_segment"，description 冻结）。禁止把 P&ID catalogue 变成隐式跨域 catalogue；runtime 只见中性 ToolDefinitionView。

## 8. Composition scope

test composition root + internal CLI/dev composition root（ CableService(store) + CableDomainAdapter + CableToolRegistry + AgentHarnessRuntime 手工组装，同 P3/P4 探针的组装方式但接真 store）。不进公开 HTTP/MCP/REST（D3/D4 或后续切片）。

## 9. 双 domain 隔离硬测（冻结 ≥8 条）

① Cable write 不改任何 P&ID document/revision/digest；② P&ID write 不改 cable envelope；③ cable id 走 P&ID loader → fail-closed；④ P&ID id 走 Cable loader → fail-closed；⑤ 全局 audit ordinal 可 P→C→P→C 交错连续；⑥ 两 domain 同时有 session/approval/tool-call 各行其道；⑦ cross-domain approval/intent/document 绑定不可串用（拿 P&ID approval 批 Cable intent → ToolIntentMismatchError）；⑧ 100 轮交错操作属性测试零污染。

## 10. D2 禁止项（本阶段与 CODE GO 均禁）

validator profile / export / UI / public REST/MCP / P&ID production behavior 修改 / runtime ports|models 修改 / audit hash-chain 修改 / schema v15 / cable_segments 生产表。

## 11. 待裁决策点

1. §2 JSON contract 与 invariant 集合；2. §4 原语六步顺序与失败矩阵口径；3. §7 cable-specific registry 方案；4. D2 CODE GO whitelist 预告：新增 cable_service.py / cable_models.py / cable_domain_adapter.py（生产）+ store.commit_cable_write + cable registry + tests/test_m11_d2_cable_runtime.py + 双隔离硬测；不动 runtime 包、不动 P&ID 模块、不动 schema。

请裁 M11-D2 Design；PASS 后请签 M11-D2 CODE GO。
