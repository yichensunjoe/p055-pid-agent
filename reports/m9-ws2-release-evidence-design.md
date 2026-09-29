# M9-WS2 Release Gate + Evidence Package · 详细设计 v2（R49-1~R49-6 全闭合，送 Gate 签 DETAILED DESIGN GO）

> 前置：WS1 已合并（main@153c28c，PR #67 squash，M9-WS1 = PASS/CLOSED）。v1 设计获 DIRECTION ACCEPTED + CHANGES REQUIRED（R49-1~R49-6），本版逐项闭合；三个待裁决策点已由 Gate 冻结（① 两态状态机认可；② **否** blob-in-JSON，改 v13 附加表 release_evidence_packages；③ evidence.zip 类别冻结为 read）。
> 执行红线：WS2 实现分支必须从 main@153c28c（或届时 exact main）fresh cut；本文档所在 docs 分支（4ee286f 后继）与 main diverged，只作设计文本来源。

## 1. Release 状态机

治理实体 `ReleaseRecord`（ReviewState.releases 元组内嵌，随 governance JSON 一体 CAS；不进任何工程 digest）：

```
release_id, document_id, state ∈ {released, superseded},
approval_id(绑 WS1 approval), engineering_revision,
review_snapshot_digest, readiness_hash(fresh, release 时重算),
evidence_manifest_hash, package_sha256,
released_by(operator), released_at, seq
```

转移（守卫不过即拒绝，语义按 §5 F1–F7 精确区分写入面）：

```
（无记录） --release[守卫全过]--> released
released   --工程 revision 漂移--> superseded（读取期派生 + 下一治理写持久化，语义同 WS1 stale）
```

- 两态无 draft（Gate 已裁认可）：release 是一次性判定，不存在半成品 release。
- superseded 不冻结、不阻止工程写；release 不是锁定。
- v1 单态 released，不引入 AFC/IFC 细分。
- 并发：release 走 governance_seq CAS 同一平面；并发双 release 一胜一 409 `release_conflict`。

## 2. Approval 消费规则

- 每个 ReleaseRecord 精确绑定一个 approval_id；release 时刻必须 `status=approved` 且 derived_liveness=live。
- release **不消费、不修改** approval（同 WS1 resolve 不失效语义）。
- superseded 后重发 release 必须走新 approval：revision 漂移已杀死旧 approval liveness，闭环靠 WS1 已冻结语义自动成立，无特判。
- release 拒绝（F1/F2/F3）不使 approval 失效——拒绝不动 review surface。

## 3. Fresh readiness gate

- release 时**重跑** `assess_document_release_readiness`，要求 `state=eligible`；decision-time hash 仅作证据。
- fresh readiness_hash 写入 ReleaseRecord。
- 测试必须真正证明 fresh rerun（R49-5）：用测试 seam 使 release-time readiness 返回 not_eligible，同时保持 engineering revision 与 review digest 不变（revision 一变会先在 F1 拦下，根本到不了 F2——v1 测试构造有此缺陷，已废），断言 readiness 被重新调用、未复用 decision_readiness_hash。生产语义不变。

## 4. Evidence package（manifest / hash / export 契约，R49-1/R49-2 闭合）

**生成时机与两阶段纪律（R49-6）**：

- **Phase A — 内存 staging（不占 SQLite 写锁）**：钉住 exact revision + exact governance_seq → fresh readiness → 生成 PDF/DXF/validation/audit projection → 构造包字节。此间 revision/governance 前进无妨——产物作废重来即可。
- **Phase B — 短事务落库**：BEGIN IMMEDIATE → 复核 documents.revision == expected_engineering_revision、复核 governance_seq == expected、复核 release 不变量 → 写 ReviewState metadata（ReleaseRecord）→ 写 release_evidence_packages 行（manifest/blob）→ 写 release.released audit → COMMIT。任一复核不过即整体回滚，内存 ZIP 丢弃。绝不落半截 release，也不为 PDF/DXF 生成持有数据库写锁。

**包成员**（文件名固定）：

| 文件 | 来源 |
|---|---|
| `drawing.pdf` / `drawing.dxf` | 既有导出面（Phase A fresh 调用） |
| `validation-report.json` | fresh 全量 validation run |
| `release-readiness.json` | fresh readiness 全量结果（含 hash） |
| `audit-chain.json` | **R49-2 冻结格式**，见下 |
| `approval.json` | WS1 approval 全字段 |
| `release.json` | ReleaseRecord 的 **evidence projection**（R49-1：不含 evidence_manifest_hash / package_sha256 / package_blob） |
| `MANIFEST.sha256` | `<sha256hex><两个空格><filename>`，文件名 ASCII 升序，LF，末行换行 |

**hash 形成顺序（R49-1 消环，顺序固定）**：
1. 生成普通成员字节（含 release.json projection——此时 manifest/package hash 尚不存在，物理上不可能自引用）
2. 对每个普通成员算 SHA256
3. 生成 MANIFEST.sha256
4. `evidence_manifest_hash = sha256(MANIFEST bytes)`
5. 生成 ZIP（成员 + MANIFEST）
6. `package_sha256 = sha256(final ZIP bytes)`
7. Phase B 事务内持久化：ReleaseRecord metadata（含 evidence_manifest_hash、package_sha256）+ ZIP blob + release.released audit

字段名冻结为 `evidence_manifest_hash` 与 `package_sha256` 两个，不再用 evidence_package_hash。

**audit-chain.json 冻结格式（R49-2）**：
```json
{
  "scope": "document-subset",
  "verified_through_ordinal": 0,
  "verified_global_tip_hash": "...",
  "global_chain_verification": "...",
  "document_records": []
}
```
先对**完整 global chain** 验证到 ordinal N，包内只携带该 document 的 subset，明确标注这是经 global-chain verification 的 document projection——不假装 subset 可独立 chain verify（audit 是全库一条全局 hash chain）。
**cutoff 纪律**：包内 audit cutoff = release audit 之前——release.released 自身不进本包 audit-chain.json（它要绑定最终 package hash，放进包内又成环）。正确闭环：cutoff → 算 manifest/zip hash → 事务写 Record+blob → **同事务**写 release.released audit（evidence 含 release_id / evidence_manifest_hash / package_sha256 / verified_through_ordinal / verified_global_tip_hash）。

**导出端点（R49-3 冻结，REST 复数）**：
- `POST /api/v2/documents/{document_id}/releases`（operator；执行守卫+两阶段落库）
- `GET /api/v2/documents/{document_id}/releases/{release_id}/evidence.zip`（纯读；superseded 历史包一一对应可导出）
- review 视图 `_view` 增 `releases` 投影（含 derived_superseded）。

**导出语义**：读已存字节，不重算；重复 GET 字节级一致（硬测）；打开前校验 blob 内 MANIFEST 与成员 hash 自洽，不一致 500 `release_evidence_corrupt`（仅迁移/手工篡改 DB 时可能）。

## 5. Failure modes（R49-4 精确化写入面）

| # | 场景 | HTTP | ReleaseRecord | blob | governance mutation | audit |
|---|---|---|---|---|---|---|
| F1 | approval 非 approved / liveness 非 live | 422 `release_approval_not_live` | 0 | 0 | 0 | **1 条 release.denied** |
| F2 | fresh readiness 非 eligible | 422 `release_readiness_not_eligible` | 0 | 0 | 0 | **1 条 release.denied** |
| F3 | 存在 open/reopened 线程 | 422 `release_open_threads` | 0 | 0 | 0 | **1 条 release.denied** |
| F4 | 非 operator / 非 loopback / shared 部署 | 403 `actor_not_trusted` | 0 | 0 | 0 | **0——不可信调用者不得向治理 audit chain 注入记录** |
| F5 | Phase A 包生成中途失败 | —（不进入 Phase B） | 0 | 0 | 0 | 0（整体未发生） |
| F6 | 并发双 release CAS 竞争 | 409 `release_conflict` | 0 | 0 | 0 | 0——竞争失败非治理拒绝决定 |
| F7 | Phase B revision/governance 复核不过 | 409 `release_conflict` | 0 | 0 | 0 | 0（同 F6） |
| F8 | （合并入 F1–F3 行）拒绝即治理事实，落 denied audit；status 用既有 AuditStatus rejected/failed 承载 | | | | | |
| F9 | superseded 后请求 release | 允许（新 revision+新 approval 走完整守卫），旧包按 release_id 导出 | | | | |
| F10 | v12 旧库 | 零 release 记录直读；release 前须先有 approval，守卫保证顺序 | | | | |

## 6. 数据面 / 迁移（R49 决策②冻结：v13 附加表，blob 不进 JSON）

- `ReleaseRecord` 仍在 `ReviewState.releases`（governance CAS 语义不变）。
- **store v13 附加表**（additive，无 FK 级联，同 review 面纪律）：
```sql
release_evidence_packages(
  release_id PK, document_id,
  manifest_sha256, package_sha256,
  package_blob BLOB, created_at)
```
  只存不可变证据字节；metadata 在 governance JSON。
- metadata + BLOB + release.released audit **同一 SQLite transaction** 写入。
- 迁移：v12→v13 additive；旧库 release 包数为零；database_recovery required_tables 增表。
- audit Literal 扩 3 事件：`release.released` / `release.superseded` / `release.denied`（只加不改）。
- surface_contract（R49 决策③冻结）：POST /releases = `governance_write` / audited=True；GET evidence.zip = `read` / has_side_effect=False / audited=False（governance_write ∈ AUDITED_CATEGORIES 的机器锁已禁止 audited=False 的 governance_write 绑定，read 是唯一自洽类别）。

## 7. API / UI / 测试矩阵

**API**：POST /documents/{id}/releases（operator token）；GET /documents/{id}/releases/{release_id}/evidence.zip（纯读）；review 视图增 releases 投影。

**UI**（ReviewPanel 增量）：操作员「发布 Release」按钮（approval live 且 fresh eligible 可用）+ 应用内确认对话框；release 状态横幅（released 绿 / superseded 灰 + 触发 revision）；按 release_id 的 evidence 下载链接与历史列表。

**测试矩阵（13 条硬锁，F2 按 R49-5 重修）**：
① F1：无 approval / stale / superseded approval → 422 + release.denied 恰好一条 + approval 原状；
② F2（R49-5 seam 构造，revision/digest 不变，fresh readiness 返回 not_eligible）→ 422 + 断言 readiness 被重新调用且未复用 decision hash + release.denied；
③ F3 open thread → 422 + release.denied；
④ F4 agent 无 token → 403 + **audit 零增长**（不可信不注入）；
⑤ F5 包生成失败注入 → Phase B 未发生、零 Record 零 blob 零 audit；
⑥ F6 双 release 并发 → 一胜一 409 + 败方无 denied audit；
⑦ F7 Phase B revision 竞态复核 → 409；
⑧ release.released audit evidence 五元组（release_id/evidence_manifest_hash/package_sha256/verified_through_ordinal/verified_global_tip_hash）齐全；
⑨ superseded 后重 release 需新 approval（F9 语义）；
⑩ v12→v13 迁移：旧库零包可读、升级幂等；
⑪ 双 GET 字节一致 + MANIFEST 自洽校验 + corrupt 路径 500；
⑫ evidence_manifest_hash / package_sha256 写回一致 + R49-1 顺序钉死（release.json 内无 hash 字段，突变测试：往 release.json 塞 hash → 拒）；
⑬ e2e：approve → release → 按 release_id 下载 zip → 脚本校验 MANIFEST 与成员 hash。

## 8. Exact 白名单（R49 修订版）

**改**：
- `backend/agentcad/review_models.py`（+ReleaseRecord、ReviewState.releases）
- `backend/agentcad/review_service.py`（+release_document 两阶段与守卫；_persist 复用）
- `backend/agentcad/store.py`（save_review_state 增 expected_engineering_revision 复核；release_evidence_packages 读写）
- `backend/agentcad/database_recovery.py`（**v13 additive BLOB 表**）
- `backend/agentcad/audit_models.py`（+3 Literal）
- `backend/agentcad/surface_contract.py`（+2 绑定：POST=governance_write/audited、GET=read）
- `backend/agentcad/api_review.py`（+2 端点与 _view releases 投影）或独立 `api_release.py`（新，包生成/MANIFEST/audit projection/校验）
- `backend/agentcad/main.py`（注册）
- `frontend/ReviewPanel.tsx` / `styles.css` / api 封装；`frontend/e2e/review.spec.ts`（扩 release 链）

**新增**：`backend/tests/test_m9_release_workflow.py`；改 `backend/tests/test_database_recovery.py`（v12→v13、旧库零包）；改 `backend/tests/test_surface_contract.py`（POST=governance_write/audited、GET=read/no-side-effect 机器锁）。

**不动**：WS1 已冻结语义、工程写通道、PDF/DXF 导出内核、store v12 已有结构（v13 纯 additive）、frontend tab 结构。

## 9. 实现纪律（执行红线复述）

- 实现分支 **fresh cut from main@153c28c**（或届时 exact main）；4ee286f 及其后继 diverged worktree **禁作代码基线**，设计文本以 cherry-pick/复制方式带入。
- 未获 DESIGN GO 不动代码；本版闭合 R49-1~R49-6 后申请 M9-WS2 DETAILED DESIGN GO → GO 后 CODE IMPLEMENTATION GO。
