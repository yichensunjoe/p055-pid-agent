# M9-WS2 Release Gate + Evidence Package · 设计草案（DRAFT — 待 WS1 合并后报 Gate 签 DESIGN GO）

> 依赖：WS1 的 approval/review facts（PR #67）。本草案按 Gate 已冻结的方向预写，正式送审前不发。

## 1. Release 状态机（消费 WS1 facts）

- 新治理实体 `ReleaseRecord`（与 EngineeringApproval 同面存储，不进工程 digest）：`document_id, release_id, state ∈ {draft → released → superseded}, released_at, released_by(operator), approval_id(绑 WS1 approval), readiness_hash(decision-time), review_snapshot_digest, evidence_package_hash`。
- 转移守卫（release 一次性判定，全部硬失败）：
  1. 引用的 WS1 approval `status=approved` 且 `derived_liveness=live`（revision 未漂、未被 reopen 失效）；
  2. **fresh readiness 重跑** `state=eligible`（WS1 决策时的 hash 是证据，release 时重算——WS1 冻结原文"WS2 真正进入 release state 时还要再做一次 fresh readiness gate"）；
  3. 当前无 open/reopened 线程；
  4. operator token 有效（同 WS1 actor trust）。
- 工程 revision 在 release 后仍可继续修改 → 已有 release 自动 `superseded`（读取期派生 + 下一治理写持久化，同 WS1 stale 语义）。release 不冻结图纸（M9 验证的是"经审核后具备进入正式流程的能力"，不是锁定）。
- 不引入 AFC/IFC 细分（Gate 明确留 WS2 之外或另行裁定——v1 单态 released）。

## 2. Evidence Package 导出

- 单包 artifact（ZIP，确定性内容）：`drawing.pdf` + `drawing.dxf`（既有导出面复用）+ `validation-report.json`（fresh run 全量结果）+ `release-readiness.json`（fresh hash）+ `audit-chain.json`（该 document 全量 audit + chain verify 结果）+ `approval.json`（WS1 approval 四元组）+ `release.json`（ReleaseRecord）+ `MANIFEST.sha256`。
- 包内全部内容来自 fresh 重跑/读取（导出即取证，不接受调用方传入 hash）；`evidence_package_hash = sha256(MANIFEST)` 写回 ReleaseRecord。
- 导出端点：`POST /api/v2/documents/{id}/release`（执行守卫+置 released）与 `GET /api/v2/documents/{id}/release/evidence.zip`（纯读导出；已 superseded 的 release 仍可导出历史包——audit 语义）。

## 3. 数据面 / 迁移

- store v13 迁移：`release_records` 表（无 FK 级联，同 review 面纪律）；additive，旧文档零 release records。
- audit 事件：`release.requested` / `release.released` / `release.superseded`（扩封闭 Literal，只加 WS2 类型）。
- surface_contract：`governance_write` 类别下增 2 条绑定（复用 WS1 已追认的类别机制）。

## 4. 测试矩阵（硬锁）

① 未批准/失效 approval 不能 release；② release 时 fresh readiness not_eligible 拒绝（即使决策时 eligible——构造：release 前新增 blocker 元素）；③ release 后改图 → superseded 派生；④ 双 release 竞争 CAS；⑤ 包内容确定性（双导出逐字节一致）；⑥ MANIFEST hash 写回一致；⑦ 旧库（v12）迁移零记录；⑧ e2e：approve → release → 下载包 → 校验 MANIFEST。

## 5. 白名单预告（正式报审给 exact list）

store/database_recovery/audit_models/surface_contract/api_release.py(新)/release_models.py(新)/review_service 小扩（release 守卫读 approval）/ tests / ExportPanel 增量。不动：WS1 已有语义、工程写通道、PDF/DXF 导出内核。
