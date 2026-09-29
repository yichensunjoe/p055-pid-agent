# M9-WS2 Release Gate + Evidence Package · 详细设计（送 Gate 签 DETAILED DESIGN GO）

> 前置：WS1 已合并（main@153c28c，PR #67 squash，M9-WS1 = PASS/CLOSED）。本设计按 Gate 上轮冻结的下一步清单逐项给出：release 状态机 / approval 消费规则 / fresh readiness gate / evidence package manifest+hash+export 契约 / failure modes / migration+API+UI+test 矩阵 / exact 白名单。

## 1. Release 状态机

治理实体 `ReleaseRecord`（与 EngineeringApproval 同面、同存储——作为 `ReviewState.releases` 元组内嵌，见 §6；不进任何工程 digest）：

```
release_id, document_id, state ∈ {released, superseded},
approval_id(绑 WS1 approval), engineering_revision,
review_snapshot_digest, readiness_hash(fresh, release 时重算),
evidence_package_hash, released_by(operator), released_at, seq
```

转移（全部 fail-closed，守卫不过即整体拒绝、零写入）：

```
（无记录） --release[守卫全过]--> released
released   --工程 revision 漂移--> superseded（读取期派生 + 下一治理写持久化，语义同 WS1 stale）
```

- 状态机刻意不设 draft：release 是一次性判定，不存在"半成品 release"。
- superseded 不冻结、不阻止工程写；release 不是锁定（M9 验证的是"经审核后可进入正式交付流程的能力"）。
- v1 单态 released，不引入 AFC/IFC 细分（Gate 已留 WS2 之外）。
- 并发：release 走 governance_seq CAS 同一平面（ReviewState 扩展），两个并发 release 只有一个落地，另一个 `release_conflict`（409）。

## 2. Approval 消费规则

- 每个 ReleaseRecord 精确绑定一个 `approval_id`；该 approval 在 release 时刻必须 `status=approved` 且 derived_liveness=live（revision 未漂 + review digest 未动 + 未被 invalidate）。
- release **不消费、不修改** approval：approval 保持 approved 原状（同 WS1 resolve 不失效语义）。release 与 approval 是"引用"不是"转移"。
- superseded 后重发 release 必须走**新 approval**：revision 漂移已杀死旧 approval 的 liveness，新 revision 上的 release 天然要求新决策——闭环不靠特判，靠 WS1 已冻结的 liveness 语义自动成立。
- release 拒绝（守卫不过）不使 approval 失效——拒绝不动 review surface。

## 3. Fresh readiness gate

- release 时**重跑** `assess_document_release_readiness`，要求 `state=eligible`；WS1 决策时 hash 只是证据，不作数（WS1 冻结原文"WS2 真正进入 release state 时还要再做一次 fresh readiness gate"）。
- fresh `readiness_hash` 写入 ReleaseRecord；与 decision-time hash 不要求相等。
- 跨平面竞态收尾：工程 revision 可能在 readiness 重跑与落库之间前进。release 持久化在**同一 SQLite 事务**内复核 `documents.revision`（store 层 `expected_engineering_revision` 校验，不过即 ReviewStateConflict→409 release_conflict）——守卫、状态、证据包、audit 一条事务，无窗口。

## 4. Evidence package（manifest / hash / export 契约）

**生成时机**：release 事务内一次性生成并**整包存储**（zip 字节存 ReleaseRecord.package_blob）；导出是纯读已存字节——取证冻结在判定时刻，不接受事后重算，也不接受调用方传入 hash。

**包内容**（文件名固定、确定性排序）：

| 文件 | 来源 |
|---|---|
| `drawing.pdf` | 既有 PDF 导出面复用（release 事务内 fresh 调用） |
| `drawing.dxf` | 既有 DXF 导出面复用 |
| `validation-report.json` | fresh 全量 validation run 结果 |
| `release-readiness.json` | fresh readiness 全量结果（含 hash） |
| `audit-chain.json` | 该 document 全量 audit 记录 + chain verify 结果 |
| `approval.json` | WS1 approval 全字段（决策四元组+hash） |
| `release.json` | ReleaseRecord 全字段（不含 blob） |
| `MANIFEST.sha256` | 见下 |

**MANIFEST 契约**：每行 `<sha256hex><两个空格><filename>`，文件名按 ASCII 升序，LF 行尾，末行有换行。`evidence_package_hash = sha256(MANIFEST.sha256 的内容)`（hash-of-hashes），在 release 事务内写回 ReleaseRecord。

**导出端点**：`GET /api/v2/documents/{id}/release/evidence.zip` 纯读（任何有文档读权限的 actor，含 agent——证据是给人看的，不是治理写）；superseded 的 release 照常导出历史包（audit 语义）。重复 GET 字节级一致（读同一份存留字节，硬测锁定）。

**导出失败/包损坏语义**：export 端点打开前先校验 blob 内 MANIFEST 与成员 hash 自洽，不一致 500 `release_evidence_corrupt`（落库时不可能发生，迁移/手工篡改 DB 时才可能出现）。

## 5. Failure modes（逐项闭合）

| # | 场景 | 行为 |
|---|---|---|
| F1 | approval 非 approved / liveness 非 live | 422 `release_approval_not_live`，零写入，approval 原状 |
| F2 | fresh readiness 非 eligible | 422 `release_readiness_not_eligible`，零写入 |
| F3 | 存在 open/reopened 线程 | 422 `release_open_threads`，零写入 |
| F4 | 非 operator（无 token/非 loopback/shared 部署） | 403 `actor_not_trusted`，零写入 |
| F5 | 守卫全过但包生成中途失败（PDF/DXF/validation 抛错） | 整事务回滚：无 ReleaseRecord、无 audit、无半截状态 |
| F6 | 并发双 release（同 governance_seq 竞争） | CAS 失败者 409 `release_conflict` |
| F7 | readiness 重跑后 revision 前进（跨平面竞态） | 事务内 revision 复核失败 → 409 `release_conflict`（同 F6 码） |
| F8 | 守卫拒绝 | 记一条 `release.denied` audit（status=denied，evidence 含失败守卫码）——拒绝也是治理事实；approval 不失效 |
| F9 | superseded 后请求 release | 允许（新 revision + 新 approval 走完整守卫），旧包仍可导出 |
| F10 | v12 旧库（WS1 记录存在/不存在） | 零迁移直接可读；release 前需先有 approval，守卫自然保证顺序 |

## 6. 数据面 / 迁移

- **不加表、不升 schema 版本**：releases 内嵌 `ReviewState.releases: tuple[ReleaseRecord, ...]`，随 governance JSON 一体存储与 CAS。旧 v12 文档的 data_json 无 releases 字段 → pydantic 默认空元组，天然零记录零迁移。比草案预告的 release_records 表更简：单一 CAS 平面、无新并发语义、无迁移风险。
- 已知取舍：blob 使 governance 行变大，每次治理写重写整行（含 blob）。图纸规模下可接受；若未来成为瓶颈，拆表是向后兼容的纯存储优化（JSON 字段保留，blob 外置）。
- audit Literal 扩 3 事件：`release.released` / `release.superseded` / `release.denied`（只加不改 WS1 已有 7+2 类型）。
- surface_contract：`governance_write` 类别（WS1 已追认）下增 2 条绑定——POST release（audited）、GET evidence.zip（read 类别？不——纯读但声明 governance_write 更准确：它读治理产物；类别 `read`+notes，或 governance_write audited=False。裁决点：建议 governance_write / audited=False / has_side_effect=False，与 WS1 GET review 视图同处理）。

## 7. API / UI / 测试矩阵

**API**（prefix /api/v2，operator token 同 WS1）：
- `POST /documents/{id}/release`（body: expected_governance_seq）→ 守卫+落 released+生成包，返回 review 视图（含 releases）。
- `GET /documents/{id}/release/evidence.zip` → 纯读流式下载。
- review 视图 `_view` 增 `releases` 投影（含 derived_superseded 派生标记）。

**UI**（ReviewPanel 增量，不新开 tab）：
- 操作员可见「发布 Release」按钮（approval live 且 fresh eligible 时可用），点击弹应用内确认对话框（不用 window.prompt）；
- release 状态横幅（released 绿 / superseded 灰 + 触发 revision）、evidence 下载链接、历史 release 列表。

**测试矩阵（硬锁）**：
① 无 approval / approval stale / approval superseded → release 拒（F1）；② approval 后新增 blocker 元素使 fresh readiness 变 not_eligible → release 拒（F2，证明决策时 eligible 不算数）；③ open thread → 拒（F3）；④ agent 无 token → 403（F4）；⑤ 包生成失败注入 → 全回滚无 audit（F5）；⑥ 双 release 并发 CAS 一胜一 409（F6）；⑦ revision 竞态复核 → 409（F7）；⑧ 每次拒绝落 release.denied audit 且 approval 不失效（F8）；⑨ superseded 后重 release 需新 approval（F9 语义）；⑩ v12 旧库直读（F10）；⑪ 双 GET 字节一致 + MANIFEST 自洽校验；⑫ evidence_package_hash 写回一致；⑬ e2e：approve → release → 下载 zip → 脚本校验 MANIFEST 与成员 hash。

## 8. Exact 白名单

**改**：review_models.py（+ReleaseRecord、ReviewState.releases）、review_service.py（+release_document 与守卫，_persist 复用）、store.py（save_review_state 增 expected_engineering_revision 可选复核；零迁移）、audit_models.py（+3 Literal）、surface_contract.py（+2 绑定）、api_review.py（+2 端点与 _view releases 投影）、api_release.py（新，包生成/MANIFEST/校验）、main.py（注册）、frontend ReviewPanel.tsx/styles.css/api 封装、frontend/e2e review.spec.ts（扩 release 链）。

**新增**：backend/tests/test_m9_release_workflow.py。

**不动**：WS1 已冻结语义（reconcile/失效/token/状态机一字不改）、工程写通道、PDF/DXF 导出内核、store schema 版本（维持 12）、frontend tab 结构。

## 9. 待 Gate 裁的 3 个设计决策点

1. §1 状态机两态（released/superseded，无 draft）——是否认可；
2. §6 releases 内嵌 ReviewState（不升 schema、不加表）vs 草案预告的 release_records 表——取更简方案是否认可；
3. §6 evidence.zip 的 surface 类别（governance_write/audited=False vs read）——请冻结。
