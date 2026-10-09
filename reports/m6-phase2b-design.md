# M6-2B-D1 — Integration Contract Design：真实 DXF → 人工确认 → 受治理工程写入

> 状态：DESIGN FINAL FIX（D87-1~D87-5 修订版，docs-only）。在 Gate 对 D1 的 Design Gate 签署前，
> 本文所有结论均为 proposal。Gate 2026-10-09 复核裁定：CHANGES REQUIRED（五项）+
> Q1–Q4 已裁（见 §10 裁定记录），并签 **M6-2B-D1 DESIGN FINAL FIX GO — APPROVED**（限 #87 原四文件范围）。
> 授权依据：Gate 2026-10-09 方向裁定——「M6 — Governed Semantic Ingestion（恢复建设），不创建 M14」，
> 并签 **M6-2B-D1 DESIGN PREP GO — APPROVED**（首片只允许设计准备，不允许工程数据写入）。
> base = main@8331de1（M13 closeout 后）。Charter 依据：PROJECT_CHARTER v1.2.0 §51。

## 0. 范围与硬约束（D1 冻结口径，Gate 原文）

**允许修改范围**：`reports/m6-phase2b-design.md`（本文件，新建）、`docs/m6-governed-semantic-ingestion.md`
（必要设计补充）、`docs/architecture.md`（必要的现状对账）、`HANDOFF.md`（治理记账）。

**D1 禁项（Gate 原文）**：禁止新增运行时写路径、HTTP/MCP write surface、数据库 migration、自动批准、
直接修改生产数据、引入第三域、改造 audit hash/ordinal、修改 M13 atomic executor，
或声称模型高置信度即可跳过人工。

**Gate 对后续分片的口径**：「D2–D5 是顺序与责任边界，不是现在一次性授权全部代码实施」；
「D1 Design Gate 通过后，才开放 D2 CODE GO」；每个代码分片仍须 exact-head CI + 独立 Merge Gate。

## 1. Gate 裁定输入（本设计回答的对象）

方向裁定结论：项目技术路线**没有偏离初心**，但存在「治理和平台能力先于真实工程输入与交付能力成熟」
的结构性失衡；不启动第三工程域，不把扩大通用 Harness 抽象层作为下一阶段方向。P0 欠债前两项：
① M6 语义摄取链尚未真正贯通（没有 external source → human-confirmed → governed engineering write 的
完整运行链）；② 真实工程资格证据缺口（WS3B，Owner-blocked，本 milestone 不解、也不依赖其解封）。

**M6 完成标志（Gate 冻结，八条）**：

| # | 验收条件 | 必须取得的证据 |
|---|---|---|
| 1 | External Source Intake | 至少一种真实外部 P&ID 格式成功进入来源与区域证据链；建议以 DXF 为首选 |
| 2 | Semantic Candidate | 设备、位号等候选独立于正式工程数据，含 source revision、region、producer 和 evidence |
| 3 | Human Confirmation | 审阅者确认的是事实，不是裸 patch；拒绝、歧义、冲突都有稳定状态和证据 |
| 4 | Governed Engineering Apply | 经人工确认的语义通过既有 apply-v2 治理边界写入；没有第二 writer |
| 5 | Semantic Reconstruction | 至少重建一个包含工程对象、位号及可验证连接关系的真实 P&ID 局部场景，不能只复现几何 |
| 6 | Provenance / Undo / Replay | source → candidate → reviewer → patch → revision 全链可追；可补偿撤销，并重放验证 canonical patch 与语义后态 |
| 7 | Gold Corpus & Qualification | 独立人工标注的小规模代表性语料，涵盖任务书冻结的七类维度；有拒识、歧义与错误分布报告 |
| 8 | Full Non-Regression | P&ID、M4/M5、CAD import、M10–M13、browser/shared-mode 全量回归，并保留原有治理边界 |

边界声明（Gate 原文）：没有可合法使用的真实外部 P&ID 文件和独立专业标注时，可以完成技术阶段，
但**不得宣称 M6 Final Acceptance**；合成数据只能证明流程可运行。

## 2. Phase-2A 已实现/未实现矩阵（逐项引用真实文件、接口和测试）

### 2.1 已实现（main@8331de1 实测在位）

| 能力 | 位置与接口 | 测试 |
|---|---|---|
| 治理契约数据（八层链 / 7 态 15 边状态机 / 8 条禁止边 / 权限声明 / 持久身份） | `backend/agentcad/m6_ingestion_contract.py:71-172`（`INGESTION_LAYERS`）、`:175`（`SOLE_WRITE_LAYER="apply_v2_transaction"`）、`:439-535`、`:599-630`（`PERSISTENT_IDENTITIES` 五条全 64 hex） | `tests/test_m6_ingestion_contract.py` 37 条（含 13 个 monkeypatch 变异） |
| 契约自校验 | `m6_ingestion_contract.py:807-1080` `validate_contract()`（约 18 组断言：唯一写入层、producer 无授权、空白名单入 confirmed 必须带 reviewer_action+review_decision、乐观并发、replay 两层、身份前缀 round-trip 等） | 同上 |
| producer 权限模型（三来源全部 may_grant_apply_authority=False、requires_human_review=True） | `m6_ingestion_contract.py:338-379`；`AUTO_ACCEPT_WHITELIST = ()` `:383`；`CONFIDENCE_CAN_AUTHORISE_A_WRITE = False` `:414` | 契约测试 `:828-841` |
| Candidate / ReviewDecision / ConfirmedSemanticFinding / StructuredEngineeringPatch schema（extra=forbid） | `m6_candidate_models.py`：`SourceArtifactRef:115`、`SourceRegion:138-168`（三类 selector + page/layer/coordinate_frame）、`ProposedSemantics:171-201`、`Confidence:204-229`（measured 类需 gate id）、`SemanticCandidate:279-336`、`ReviewDecision:339-393`、`ConfirmedSemanticFinding:396-440`（`provenance_chain` 前四项=artifact→region→candidate→decision，`:425-437`）、`StructuredEngineeringPatch:443-474`（`write_authority="none"`，`:49`） | `tests/test_m6_candidate_core.py` 48 条 |
| 治理核心服务：file（白名单必须为空）/ 状态回放 / confirm（decision+finding 原子）/ reject / recheck_baseline（乐观并发）/ resolve_conflict / supersede | `m6_candidate_core.py` `M6CandidateService:354-893`；回放 `:379-397`；confirm 原子 `:533-562`；`REVIEW_DECISION_ID_VERSION="review-decision-v1"`（`:941`，`decided_at` 排除在身份外 `:944-1002`） | 含 append-only、跨 candidate composite FK、确认原子性故障注入等（`:885/:893/:905/:964/:995`） |
| 确定性 patch 编译器（两意图：`creation` / `metadata_enrichment`；元素 id=`el_m6`+digest；path-aware 规范化，未登记 path 硬失败 `unregistered_semantic_path`） | `m6_candidate_core.py:75`（`PHASE_2A_SUPPORTED_INTENTS`）、`compile_finding:726-783`、`_operations_for:785-850`、`_disposition_for:912-918` | 编译幂等/digest 排除 volatile（`:632/:648`）、正例到 patch 为止（`:689`） |
| 拒绝语义（稳定 reason code） | `unresolved_carries_no_fact` / `relationship_addition_not_compiled_in_phase_2a` / `symbol_class_change_not_supported` / `metadata_enrichment_requires_target` / `target_not_resolved` / `overwrite_requires_human_resolution`（`:831-836`）/ `already_stated` / `out_of_catalogue` / `creation_requires_geometry` / `unknown_intent` | 48 条内逐项覆盖 |
| 持久化三表（schema v7，insert-only；`review_status_at_creation` 只存出生态；无指向 documents 的 FK，文档删除不级联；findings 对 decisions composite FK RESTRICT） | `database_recovery.py` `_migration_7:854-952`（`semantic_candidates:883-897`、`review_decisions:904-927`、`confirmed_semantic_findings:930-952`）；当前 `CURRENT_SCHEMA_VERSION = 16`（`:19`，v16=M13 `project_change_sets`） | `tests/test_m6_candidate_core.py:507/:528/:538/:598` |
| store 原语（无更新路径即不可变强制；`record_confirmation` 同事务双写） | `store.py:2314-2496`（`insert_semantic_candidate:2322`、`insert_review_decision:2370`、`list_review_decisions:2374`、`record_confirmation:2393-2415`、`insert/get/list_confirmed_finding(s):2471-2485`） | 同上 |
| 端到端 walkthrough 证据（正/负/确定性/级联四条） | `scripts/m6_phase2a_walkthrough.py`；`reports/m6-phase2a/walkthrough.txt` | 由 `tests/test_m6_candidate_core.py:1249` 钉死 |

### 2.2 刻意断点（Phase-2A 设计如此，不是缺陷）

- `request_apply()` 恒抛 `GovernedWriteNotAuthorized(code="apply_not_authorized_in_phase_2a")`
  （`m6_candidate_core.py:883-890`）；`_KIND_TARGET_STATUS` 含 `applied` 即 RuntimeError（`:340-344`）。
- 表层令牌冻结：`PHASE_1_FORBIDDEN_SURFACE_TOKENS`（`m6_ingestion_contract.py:777-784`：
  `candidate · ingestion · ingest · semantic-finding · confirmed-finding · review-queue`），
  由契约测试对着活表层（OpenAPI + MCP 工具枚举）断言缺席（`test_m6_ingestion_contract.py:281-309`）。

### 2.3 未实现（本设计要接续的缺口；每条附排查方式）

| 缺口 | 排查证据 |
|---|---|
| 无 HTTP/MCP 表层（candidate/review/apply 均无） | 穷举 19 个 `api_*.py` 路由 + `mcp_server.py` 34 个 `@mcp.tool()`，无相关端点；无 `api_m6*` 文件 |
| 无 apply-v2 连接 | 全库 grep `m6_candidate`/`m6_ingestion`：命中仅 m6 自身、`store.py:23-26`、两个测试、walkthrough 脚本；`service.py`/`api_v2.py` 零引用 |
| 无真实 artifact → candidate 适配器 | `cad_import.py`/`api_cad_import.py`/`cad_dxf.py` 均不引用 m6 模块；walkthrough 的 candidate 是手工 fixture（`scripts/m6_phase2a_walkthrough.py:60` 起） |
| 无 review queue UI | `frontend/src` grep 无命中；既有 `frontend/src/editor/ReviewPanel.tsx` 是 M9 工程审阅（thread/comment/approval/release），非 M6 候选队列 |
| 无 patch 持久化 | patch 只在内存（`compile_finding` 即时产出即时弃）；无 `m6_*patch*` 表 |
| 无 replay harness / 补偿 undo 实现 | 契约仅有常量（`UNDO_MECHANISM` `m6_ingestion_contract.py:753`、`TRANSACTION_STATES :548`）；store 无 revert 方法 |
| 无 gold corpus | `backend/tests/m6_gold_corpus/`、`research_examples/`、`candidate_seed_material/` 均不存在（ls 验证），路径仅在契约声明（`:723-727`） |
| 无 calibration 记录 | `measured_on_gold_corpus` 默认禁止，需独立 Calibration Gate（任务书 §5 / 契约 `:416-433`） |

### 2.4 调研中发现的既有不一致（仅登记，D1 不改）

- `docs/cad-import.md:23` 声称支持二进制 DXF，但 `cad_dxf.py:183-187` 实际只读文本 DXF——文档与代码矛盾。
  **Gate 已路由：列为 P1 documentation correctness debt，下次正常文档维护独立修正；D2 只按
  已核实支持的 DXF 子集设计，不因文档措辞推断能力**（Gate 2026-10-09 复核第五节）。
- 文字旋转/MTEXT 行内格式不保留（`cad_dxf.py:656-664`；`docs/cad-import.md:193`）——对 tag 提取的影响：
  文字锚点（插入点）与内容在，可作 `text_spans` selector；旋转角丢失意味着「竖排位号」的区域重定位精度降级，
  列入 §7 gold corpus 的 `acceptable_ambiguity` 标注维度。
- 真实 DWG 试验的元素统计有两套口径（源 primitive 计数 9053+704 弧/椭圆 = 结果文档按 element.type 9757），
  引用时必须注明口径（`reports/m5/repair-scale-real.json` vs `docs/m5-agent-self-repair.md:183`）。

## 3. 目标数据流与权限图

**双文档身份（D87-1 冻结）**：摄取始终区分两种文档身份——

- **Source（证据身份，只读）**：`source_document_id + source_revision + source_content_hash`。
  导入产物自摄取钉死起**视为不可变证据**：后续工程修改不得写回该文档；要在更新版原图上重新摄取，
  只能导入产生**新的** source 文档/ revision，从而产生新的 artifact/region/candidate 身份，
  旧身份链原样封存（不编辑、不迁移、不静默移动）。
- **Target（工程写入目标，可增长）**：`target_document_id + expected_target_revision`。
  目标可以是新建的工程重建文档，也可以是既有工程文档（富化场景）。target revision 随每次
  受治理提交正常 +1；**target 前进不使候选失效**——候选只钉 source 证据身份，
  对 target 的一致性由 baseline digest + CAS 在 apply 时刻重新核算（§4.5）。

```text
DXF 文件（外部真实输入）
  │  ① 既有 cad_import 受治理导入（一次导入=1 revision/1 history/1 audit/1 undo；
  │     源 SHA-256 入 metadata+审计；产出全几何 source 文档，0 symbol/0 connector，P0 边界保持）
  ▼
SOURCE：imported_document（documents 表，revision=R0 钉死为证据基线，
  元素带 cad_layer/cad_block/cad_handle 出处键；摄取后只读，不再作为编辑目标）
  │  ② M6 intake（D2，只读 source）：SourceArtifactRef{source_document_id, source_revision=R0,
  │     content_hash=source 内容哈希}；区域推导 → SourceRegion（三类 selector，
  │     coordinate_frame="document"）；候选声明其 target_document_id
  ▼
semantic_candidate（既有 v7 表 + D2 起 candidate payload 携带 target_document_id；
  producer=deterministic_rule_engine v1；ProposedSemantics 只写事实；confidence≠authority）
  │  ③ M6 review（D3，HTTP 读面 + 决策端点 + 最小 UI）：人工确认事实（reviewer 身份四要素 §4.3），
  │     拒绝/歧义/冲突各有稳定状态；baseline_record 对 TARGET 当时状态快照
  ▼
confirmed_semantic_finding（既有 v7 表；provenance_chain 四链）
  │  ④ compile（既有编译器，D4 扩展 relationship_addition）→ StructuredEngineeringPatch
  │     （baseline_revision = target 在编译/确认时刻的 revision）
  │     →（D4 新增）patch 持久化（§5 v17，patch 行同时记录 source/target 双文档身份）
  ▼
structured_engineering_patch（持久化，write_authority="none" 不变——它只是数据）
  │  ⑤ governed apply（D4）：M10 sealed flow（专用 M6 registry/adapter/runtime，
  │     tool=apply_m6_confirmed_finding，permission=ask/risk=engineering_change）
  │     → authorize（intent hash 绑定 source/target 双身份，服务端重算比对）
  │     → DocumentService.apply_transaction(document_id=target_document_id,
  │         expected_revision=target baseline CAS, precommit_validator)
  ▼
apply_v2_transaction → committed_revision（唯一工程写入层，契约铁律不变）
  │  ⑥ replay harness（D4）/ 补偿撤销（经 apply-v2 的补偿事务，TRANSACTION_STATES 记账）
  ▼
D5：真实图纸资格化 + gold corpus 首批 + 全量非回归 + closeout
```

**权限分级（每段）**：①导入=既有 governed（不动）；②intake/候选登记=治理面数据写（非工程写，
audited，permission 设计见 §9-D2——建议 ask/draft_edit 级，不进 DRAFT_EDIT_EXCEPTIONS 白名单理由栏除非
Gate 另批）；③review 决策=治理面写（audited；shared 模式 reviewer 身份=operator token）；⑤apply=
engineering_change + ask，shared 模式必须人工 resolve approval（沿用 M13 `approval_not_self_served`
语义，`api_change_set.py:297-308` 先例）；⑥replay=只读验证。
**唯一工程写入层保持 `apply_v2_transaction`**（`SOLE_WRITE_LAYER`，`m6_ingestion_contract.py:175`），
M6-2B 全程不新增第二 writer。

## 4. 绑定设计（Gate 要求的五项完整绑定）

### 4.1 Source revision 绑定（D87-1 修订：staleness 只钉 source，不钉 target）

- artifact = 既有导入文档（`documents` 行），`SourceArtifactRef` 已带 `source_document_id` +
  `source_revision` + `content_hash`（`m6_candidate_models.py:115-123`）。
- intake 在**观察时刻**钉死 source 三元组；`content_hash` 取该 revision 文档内容哈希
  （复用审计侧 content hash 口径，`audit.py` 的 `base/result_content_hash` 同族）。
- **source 侧 staleness**：source 文档被删除、或其 source_revision 快照不可读（历史快照不可用）时，
  该区域证据链失效——review 面显示 `source_unavailable`，apply 一律拒绝
  （稳定码 `source_snapshot_unavailable`，零写）。source revision 本身被冻结为证据基线，
  不存在「source 文档 revision 自然前进」的路径（摄取后 source 只读；要摄取新原图 = 新导入 = 新身份）。
- **target 侧不 staleness**：target 文档 revision 随受治理提交正常前进，候选**不因此失效**；
  target 一致性由 §4.5 的 baseline digest 重算 + CAS 在每次 apply 时刻独立核算，
  冲突走 `conflicted`（需新 reviewer decision），而不是 stale。
- **设计级负例（D87-1 指定）**：同一 source 产出十个候选，顺序人工确认并逐条 apply——
  第 1 条 apply 使 target R0→R1，其余九条钉的 source R0 不变、**必须仍然可继续审阅与 apply**；
  每条 apply 以自己的 baseline digest 对 target 当前态重算，互不影响（见 §6.2-N13）。

### 4.2 区域可重定位

- region 身份 = 对 `(artifact_id, source_revision, 三类 selector 的 canonical 内容)` 的确定性 digest
  （`m6reg_` + 64 hex，新身份前缀入 `PERSISTENT_IDENTITIES`）；契约已强制「同 revision 可确定性重定位，
  revision 变更新身份」。
- `element_refs` 直接引用导入元素的确定性 id（`el_cad_NNNNNN`，`cad_import.py:700`）；
  `cad_block`/`cad_handle` 出处键（`cad_import.py:688-693`）提供「同一符号实例的几何簇」天然分组，
  D2 适配器按 block 聚簇生成设备候选区域。
- `coordinate_frame` 沿用已声明的 `"document"`（`m6_candidate_models.py:157`）：重定位在文档坐标系进行，
  不反推 CAD 源坐标（源坐标系的 y 翻转/unit_scale 换算已在导入侧完成，`cad_import.py:462-474`；
  避免重走已知坑）。
- `text_spans` 引用文字元素内容与锚点（文字旋转不保留是已知限制，见 §2.4）。

### 4.3 人工身份（D87-2 修订：区分四件事，不把共享 token 伪装成个人身份）

明确区分四个概念（D3 决策面与审计证据按此四元组记录）：

1. **authentication evidence**：哪种访问凭据通过了校验。shared 模式当前是服务级
   `Authorization: Bearer <api_token>`（`security.py:95-113`）——它只证明「请求携带有效的服务访问
   凭据」，**不能映射到具体工程师个人**；如实记录为 service-token，不虚标个人身份。
2. **reviewer attribution**：操作者声明的人员身份（`reviewer_identity` 自由文本/工号），
   由客户端表单显式填写，服务端不伪造、不从 token 推导。
3. **human decision evidence**：确认内容 + 动作 + 时间 + 可追溯记录——`ReviewDecision` 人类决定
   必须带 `reviewer_action`+`reviewer_identity`，事件类决定禁止携带（`m6_candidate_models.py:372-393`）；
   decision id = `review-decision-v1` 全内容 digest（含 reviewer 身份，`decided_at` 排除）。
4. **identity assurance**：身份可信级别显式标注——`identity_assurance ∈ {declared, authenticated}`。
   现阶段（不重做 Auth）一律 `declared`：界面与证据中明示「该身份为操作者声明、未经独立认证」；
   未来若接入真实账号体系，新值 `authenticated` 才允许出现，且必须带认证来源。
   绝不允许把 `service-token + declared identity` 包装成 `authenticated`。

- local 模式：同样四元组，authentication evidence 记为 local-process，assurance 仍 `declared`。
- D3 决策端点全部走 `surface_contract.py` 声明 + audited 类别（机械测试 `test_surface_contract.py`
  守住），令牌收窄节奏按 Gate 裁定执行（§10 裁定记录 Q2）。

### 4.4 Approval 绑定

- 复用 M10 runtime 的 sealed 模式（M13 先例，`api_change_set.py:81-86`）：M6 自建
  `M6ToolRegistry`/`M6DomainAdapter`/`M6AuditAdapter` + 独立 `AgentHarnessRuntime` 实例，
  **不改默认 P&ID registry**（D86-1 教训）。
- 工具定义（D4 新增，Gate CODE GO 后登记）：`apply_m6_confirmed_finding`，
  permission=ask，risk=engineering_change（沿用 `ToolRisk` 五值，`runtime/ports.py:37`）。
- intent 绑定材料（服务端派生，apply 时重算比对，漂移即 409 零写——同 M13
  `project_change_set_runtime.py:74-90/:221-233` 模式）：`patch_id` + `canonical_digest` +
  **source 三元组（document/revision/content_hash）+ target 二元组（document/baseline_revision）** +
  `expected_revision` + finding/candidate 身份链。
  `tool_intent_hash`（`runtime/harness.py:120-132`）四元（session/tool/document/intent）精确相等才放行
  （`runtime/harness.py:427-438`），approval 单次消费（状态 approved→consumed）。

### 4.5 Baseline CAS 与 conflict 重查（绑定对象是 target，不是 source）

- patch 携带 `baseline_revision`（**target** 在编译/确认时刻的 revision 快照）；apply 对
  **target_document_id** 以 `expected_revision=baseline_revision` 走 `apply_transaction` CAS
  （`service.py:519-522`，冲突 `RevisionConflictError`→409）。
- 语义级 conflict：review 时记录的 `ConflictBaselineRecord`（digest_version="semantic-value-v1"，
  `m6_candidate_models.py:252-267`）在 apply 前对 **target 当前 committed 状态**重算
  （path-aware digest），不一致 → `conflicted`，零写；解除冲突必须产生新的 reviewer decision
  （契约 `:980-1005` 已断言）。source 的 revision 不参与此重算（它是冻结证据身份）。
- precommit_validator（B' 协议，`service.py:525-531`）：D4 在写前对暂存 target 文档重跑适用
  校验子集，失败零写（与 M7 物化器同一协议，`m7_layout_materialization.py:1292` 先例）。

## 5. 持久化裁定：需要 schema v17（两张新表；D1 不迁移）

### 5.1 裁定与理由

**裁定：M6-2B 需要一次 schema v17 迁移，新增恰好两张表**（Gate Q1：两表最小化方向**原则批准**，
DDL 在 D87-1/D87-3 闭合后随 D4 CODE GO 冻结；Gate 确认无证据要求第三张表）。

需要新持久化的最小性论证：
- `m6_structured_patches` 必需：patch 当前只在内存（§2.3），而完成标志 6 要求
  source→candidate→reviewer→patch→revision 全链可追 + replay 重放验证 canonical patch——
  patch 必须有 durable identity。本表**真 append-only**（patch 一旦产生不可变；内容变=新 digest=新行）。
- `m6_apply_records` 必需：apply 的账本（patch↔transaction↔revision↔audit 绑定、
  `TRANSACTION_STATES=(applied,reverted,superseded)` 记账）无法由现有表表达——
  `documents`/`audit_records` 是工程面真相，M6 需要自己的治理面视图（与 M12/M13 的
  project_change_sets 治理面表先例一致）。本表**不是纯 append-only**，而是**受 CAS 约束的
  生命周期账本**（D87-3）：行插入后只允许两条状态迁移边，其余字段不可变。
- **不新增** artifact 表：artifact = 既有 `documents` 行 + revision（§4.1），身份可推导；
  **不新增** region 表：region 作为值对象内嵌 candidate payload（v7 既有设计），身份由 §4.2 digest 派生。
  两张表是回答完成标志 4/6 的最小集合。
- **双文档身份入列（D87-1/D87-3）**：两表都同时携带 `source_document_id` 与 `target_document_id`，
  落库模型层面杜绝 source/target 混淆。

### 5.2 DDL proposal（v16→v17；遵循 v14→v15 先例：迁移全程 FK enforcement 保持 ON）

```sql
CREATE TABLE m6_structured_patches (
    patch_id            TEXT PRIMARY KEY,        -- 'm6patch_' + 64 hex（= canonical_digest）
    finding_ids_json    TEXT NOT NULL,           -- 有序 finding id JSON 数组（顺序进 digest）
    intent              TEXT NOT NULL,
    policy_disposition  TEXT NOT NULL,
    operations_json     TEXT NOT NULL,           -- canonical operations 载荷
    compiler_name       TEXT NOT NULL,           -- 如 'm6.patch_compiler'
    compiler_version    TEXT NOT NULL,
    compiler_rules      TEXT NOT NULL,
    baseline_revision   INTEGER NOT NULL CHECK (baseline_revision >= 0),  -- target 侧
    canonical_digest    TEXT NOT NULL,           -- 64 hex
    source_document_id  TEXT NOT NULL,           -- 证据文档（无 FK：文档删除不级联，任务书 §2.2 / 契约 :950-960）
    target_document_id  TEXT NOT NULL,           -- 工程写入目标（无 FK，同上）
    created_at          TEXT NOT NULL
);

CREATE TABLE m6_apply_records (
    apply_id            TEXT PRIMARY KEY,        -- 'm6apply_' + 64 hex
    patch_id            TEXT NOT NULL REFERENCES m6_structured_patches(patch_id) ON DELETE RESTRICT,
    candidate_id        TEXT NOT NULL REFERENCES semantic_candidates(candidate_id) ON DELETE RESTRICT,
    source_document_id  TEXT NOT NULL,           -- 无 FK（同上）
    target_document_id  TEXT NOT NULL,           -- 无 FK（同上）
    base_revision       INTEGER NOT NULL CHECK (base_revision >= 0),      -- target 侧
    result_revision     INTEGER,                 -- target 侧，成功写入后的实际 revision
    status              TEXT NOT NULL CHECK (status IN ('applied','reverted','superseded')),
    tool_intent_hash    TEXT NOT NULL,
    approval_id         TEXT NOT NULL DEFAULT '',
    audit_record_id     TEXT NOT NULL DEFAULT '',  -- 恰好一条 applied/rejected 审计的 record id
    applied_at          TEXT NOT NULL,
    reverted_at         TEXT NOT NULL DEFAULT '',
    revert_apply_id     TEXT NOT NULL DEFAULT ''   -- 补偿撤销时指向补偿 apply 记录
);
CREATE INDEX idx_m6_apply_records_patch ON m6_apply_records(patch_id);
CREATE INDEX idx_m6_apply_records_target ON m6_apply_records(target_document_id);
```

**账本不变量（D87-3 闭合项，D4 以测试钉死）**：

- **L1 身份链交叉校验（同事务）**：写入 `m6_apply_records` 的同一事务内，服务端必须：
  ① patch 行存在；② 重算 patch 的 `canonical_digest` 与 `patch_id` 一致；
  ③ `finding_ids_json` 逐条存在且回放现状为 confirmed；
  ④ 本行 `candidate_id` ∈ 这些 finding 的 `candidate_id` 集合；
  ⑤ `source_document_id` / `target_document_id` 与候选、patch 双侧一致。
  任一不满足 → 整事务回滚，零写（负例见 §6.2-N8/N14）。
- **L2 状态迁移**：仅 `applied→reverted`、`applied→superseded` 两条边，CAS 携带原状态；
  重复迁移 / 非法边 / 终态再写一律拒绝（M13 三边 CAS 先例，`store.py:1264-1310`）。
- **L3 治理账本与工程提交同事务一致**：apply 成功的账本行（`status='applied'` +
  `result_revision` + `audit_record_id`）必须与工程文档 CAS 写在**同一事务**落库；
  失败路径走独立 closeout 事务（恰好一条 rejected 审计）——不允许出现「审计已 applied
  而账本未 applied」的半态（M13 `commit_project_change_set` 单事务纪律，`store.py:1312-1536`）。
- **L4 补偿撤销**：补偿 = 一条新 apply 记录（intent=revert，经同一 sealed apply-v2 流）+
  原子同事务把原记录 `applied→reverted` 并回填 `revert_apply_id`/`reverted_at`；
  candidate 永不出现 `reverted` 状态（契约 `:548-560`）。
- 身份前缀注册：**`m6reg_`（区域身份）随 D2 登记**入 `PERSISTENT_IDENTITIES` 并同步契约测试
  （D87-5：D2 就需要产生区域身份，不能等到 D4）；**`m6apply_` 随 D4 登记**。
  两者均属契约数据变更，随对应分片 CODE GO 一并签署。

### 5.3 迁移 / 备份 / 回滚设计

- 迁移入口：`database_recovery.py` `_MIGRATIONS` 增加 `17: _migration_17`（既有映射 `:1408-1425`），
  `_validate_required_schema`（`:1443-1565`）补两表必需列校验；迁移幂等、失败即整体回滚
  （既有事务纪律）。
- 备份：沿用既有备份/恢复设施（`restore_backup` 链，M12-D2 曾加固
  `allow_pre_current_schema=True` + required-schema 门）；v17 库可恢复 v16 备份（恢复后重放迁移），
  反向（v17→v16）不支持——前向唯一，回滚 = 恢复迁移前备份。
- D1 不执行任何迁移（Gate 禁项）；DDL 在 D4 CODE GO 后随实现落地。

## 6. 正向 / 拒绝测试矩阵（D2–D5 各分片的验收锚点）

### 6.1 正向

| # | 场景 | 期望 | 分片 |
|---|---|---|---|
| P1 | DXF 导入文档 → block 聚簇区域推导 → 设备/位号候选登记 | 候选含 artifact 三元组 + region digest + producer=deterministic_rule_engine；工程状态零变化 | D2 |
| P2 | 候选 → 人工确认（带 reviewer 身份）→ finding 落库 | decision+finding 同事务；provenance_chain 四链完整 | D3（核心已在 Phase-2A） |
| P3 | finding → 编译 → patch 持久化 | patch_id 幂等（同输入同 digest）；v17 行落库 | D4 |
| P4 | patch → M10 sealed apply → 工程写 | 单事务：文档 CAS+history+audit+session/approval 关闭；apply 记录 applied；revision=baseline+1 | D4 |
| P5 | apply 后 replay | canonical patch digest 与语义后态 digest 两层一致（排除 volatile），契约 `:1036-1052` | D4 |
| P6 | 补偿撤销 | 经 apply-v2 的补偿事务成功；原 apply 记录 →reverted；candidate 无 reverted 态 | D4 |
| P7 | 真实图纸局部场景端到端（设备+位号+可验证连接） | 完成标志 5 证据链齐全 | D5 |

### 6.2 拒绝 / 负例（Gate 点名十项 + D87 修订新增 N13/N14 + 既有纪律）

| # | 场景 | 期望稳定行为 | 分片 |
|---|---|---|---|
| N1 | 未知符号（块聚簇匹配不到 catalogue） | 候选不带 symbol_class 或拒识标记；**编译拒绝** `out_of_catalogue`；绝不 fallback 整目录猜（M7 教训） | D2/D4 |
| N2 | 证据不足（候选缺 evidence 或 region 无 selector） | schema 层拒绝（`SourceRegion` 至少一个 selector，`m6_candidate_models.py:160-168`）；登记失败零写 | D2 |
| N3 | 歧义（一个区域多个同样合法读法；多端口连接歧义） | 候选标歧义态，必须人工决断；端口不猜（M7-Q2 冻结纪律：receipt→完整重述→确定性 selector） | D2/D3 |
| N4 | 错误关联（位号绑错设备簇） | 人工可在 review 改派关联；改派产生**新** decision（追加式日志，不改历史） | D3 |
| N5 | 过期 source（source 文档被删 / source_revision 快照不可读） | review 面显示 `source_unavailable`；apply 拒绝 `source_snapshot_unavailable`，零写；在更新版原图上重新导入产生全新身份链 | D3/D4 |
| N6 | baseline drift（review 后 target 侧目标语义值被改） | apply 前对 target 当前态重算 digest 不一致 → `conflicted`，零写；需新 reviewer decision 解除 | D4（核心已在 Phase-2A） |
| N7 | 重复应用（同一 patch 二次 apply / approval 重放） | approval consumed → `tool_approval_consumed`；terminal 记录不被覆盖（M13 closeout 纪律） | D4 |
| N8 | reviewer 决策缺失（finding 无 decision 引用直接进编译/apply） | composite FK RESTRICT + 服务层校验双重拒绝；零写 | D4 |
| N9 | 补偿撤销失败（补偿事务 CAS 冲突） | 原记录保持 applied；失败 closeout 恰好一条 rejected 审计；可重试 | D4 |
| N10 | 失败零脏写（apply 任意环节故障注入） | 工程面零写；治理面独立 closeout；无半提交状态（M13 `commit_project_change_set` 原子纪律 + §5-L3） | D4 |
| N11 | 无 token / 错误 token（shared 模式） | 401/403 fail-closed（`approval_not_self_served` 语义沿用） | D3/D4 |
| N12 | 模型高置信度绕过人工 | 契约层不可能：`AUTO_ACCEPT_WHITELIST=()` + `CONFIDENCE_CAN_AUTHORISE_A_WRITE=False`；任何 auto-accept 提案=契约测试红 | 全程 |
| N13 | 同一 source 十个候选顺序应用（D87-1 指定设计级负例） | 第 1 条 apply 使 target R0→R1 后，其余九条**不得**因 source 钉死而失效；每条 apply 独立对 target 当前态重算 baseline digest 与 CAS，全部可顺序完成；若中途他人改了某条的目标语义值，仅该条走 N6 conflict | D4 |
| N14 | 账本身份链伪造（apply 记录引用不属于 patch findings 的 candidate / 双侧 document id 不一致 / digest 重算不符） | §5-L1 同事务交叉校验任一不满足 → 整事务回滚零写 + 稳定拒绝码；负测逐项变红 | D4 |

## 7. 真实样本取得计划 + gold corpus 标注方案

### 7.1 真实 P&ID 样本

- **正式资格化路径（Gate Q4 裁定）**：**Owner 授权的真实 DXF 为正式资格化首选**——Owner 提供一份
  可合法使用的真实项目 P&ID（DXF 导出），登记数据授权（来源/用途/脱敏状态）。这与 WS3B 挂起项
  同源但**不等价**：本 milestone 只需要「可合法用于开发/测试语料的图纸」，不要求真实试点流程。
- **D2–D4 材料边界（Gate Q4 裁定）**：未取得授权前只使用**合成或明确获准的开发材料**；
  既有本地 `气路系统总图.dwg`（939,381 B，AC1032，SHA-256 `5e62ec5c…`，导入 9757 元素，
  `reports/m5/repair-scale-real.json`）的**历史实验结果不等于未来使用授权**。
- **合成兜底**：工程审阅过的合成 DXF（覆盖七维度的受控构造），明确标注 synthetic。
- **状态标注**：在 Owner 提供授权样本前，D2–D4 不阻塞（用合成/获准开发材料），
  **D5 的「真实图纸资格化」与 M6 Final Acceptance 保持阻塞**（Gate 边界声明 + Q4 裁定原文）。

### 7.2 gold corpus 首批标注方案

- 路径：`backend/tests/m6_gold_corpus/`（契约 `GOLD_CORPUS_PATH`）；R8 的 112×2 标注永远只是
  seed material（`SEED_MATERIAL_COUNTS_AS_EXPECTED_TRUTH = False`），不进语料。
- 每条目五字段（`GOLD_ITEM_REQUIRED_FIELDS`）：`source_crop_reference · expected_semantic_fact ·
  acceptable_ambiguity · forbidden_interpretation · review_provenance`。
- 七维度覆盖（`GOLD_CORPUS_DIMENSIONS`）：`symbol_classification · tag_association ·
  annotation_role · connection_relationship · ambiguous_no_decision ·
  conflict_with_existing_semantics · insufficient_evidence`。
- 规模与质量：第一批每维度 ≥3 条（≥21 条），**标注人 ≠ 适配器作者**；每条记录拒识/歧义/错误
  的期望分布；语料建立后**仅测量**，`measured_on_gold_corpus` 仍需独立 Calibration Gate
  （任务书 §5，不写自动晋升规则）。
- 标注流程：裁剪参考（region digest + 渲染截图）→ 独立标注表 → 双人分歧仲裁记录 →
  入库前契约字段校验。

## 8. 性能预算与证据方法提案

### 8.1 预算（冻结前的 proposal；D2 CODE GO 时以 `reports/m6-2b-perf-baseline.json` 钉基线）

| 路径 | 预算 | 依据锚点 |
|---|---|---|
| 区域推导 + 确定性候选抽取（10k 元素导入文档） | ≤ 5s（本地 median-of-5） | 同规模导入 7.4s / canonical 校验 ~0.7s（M4 实测） |
| review 列表/详情读面 | 分页 ≤200 条/页，单页查询 ≤200ms（本地） | 既有只读 GET 面无审计开销 |
| governed apply 增量开销（vs 裸 `apply_transaction`） | ≤ +10% 同事务墙钟 | M13 executor 同口径 |
| 全量后端回归 | ≤ pre-M6-2B 基线 +10% | M12/M13 同一 ceiling 纪律 |

### 8.2 证据方法（M13 教训固化）

- 性能结论一律用**同 workflow exact CI log** 对比；runner 同工作量噪声带 >±40%（实测极差 89s），
  **单次样本在 ceiling 附近不可分辨**——多样本如实呈现，交 Gate 裁定，不择优不剔除。
- 本地热路径用 median-of-5（M4 方法）；大图场景沿用 9757 元素导入文档级实测。
- 所有性能声明绑定 exact head + run id；禁止跨 workflow 比数字。

## 9. 后续分片冻结提案（顺序与责任边界；逐片等 Gate CODE GO）

### D2 — Source-to-Candidate Adapter（service 层，无表层）

- 范围：导入文档 → 区域推导（block 聚簇 + 文字锚点）→ 确定性抽取（symbol 候选匹配、tag 候选、
  annotation_role 候选）→ 候选登记（复用 `M6CandidateService.file_candidate`，payload 携带
  `target_document_id`）。
- **区域身份本片登记（D87-5）**：`m6reg_` 完整身份格式入 `PERSISTENT_IDENTITIES` + 契约测试同步，
  不等 D4。
- producer 只接 `deterministic_rule_engine`（Gate Q3 APPROVED）；bounded-judgment producer
  （TypeSafe 等）接口预留、**本片不接线**，也不建立伪造的 confidence calibration。
- 禁：HTTP/MCP 表层（**D2 零解禁**，令牌冻结原样——Gate Q2）、review UI、apply、migration。
- 完成标志：§6 P1 + N1/N2/N3 测试；walkthrough 脚本 + 报告 evidence。

### D3 — Human Review & Conflict Control

- 范围：review queue 只读 GET 面（候选列表/详情/证据区渲染数据）+ 决策端点（confirm/reject/recheck/
  改派关联）+ 最小 UI 面板；决策全审计；reviewer 身份按 §4.3 四元组记录
  （authentication evidence / reviewer attribution / human decision evidence / identity assurance），
  shared 模式如实标注 `declared`，不得把服务 token 包装成个人身份（D87-2）。
- 本片才收窄 `PHASE_1_FORBIDDEN_SURFACE_TOKENS`：**按实际已登记表层逐词收窄**（Gate Q2：
  只移除本片真实登记的令牌，不整块清空）；每个新路由同步 `surface_contract.py` 声明
  （否则 `test_every_mutating_route_is_declared` 红）。
- 禁：apply、migration、编译器变更。
- 完成标志：§6 P2 + N3/N4/N5/N11；e2e（人工确认/拒绝/冲突重决）。

### D4 — Governed Apply & Replay

- 范围：schema v17（§5 两表 + L1–L4 账本不变量）+ 编译器扩展 `relationship_addition`（连接创建：
  端点只带 symbol+port_id，坐标由 `_normalize_endpoint` 服务端重算——绝不信任抽取坐标）+
  patch 持久化 + M10 sealed apply（§4.4，intent 绑定含 source/target 双身份）+ replay harness +
  补偿撤销 + 失败 closeout（M13 双事务纪律）。`m6apply_` 前缀本片登记。
- 表层令牌：D4 只处理余下**真实需要**的令牌（Gate Q2；既有禁止清单无独立 apply 字面令牌，
  不假设需要删除）。
- 禁：改 M13 executor、改 audit hash/ordinal、改 `runtime/ports.py` 中性语义、auto-accept。
- 完成标志：§6 P3–P6 + N5–N10、N12–N14；全量非回归 + 性能证据。

### D5 — Real Drawing Qualification & Closeout

- 范围：真实授权样本局部场景端到端（完成标志 5）+ gold corpus 首批（§7.2）+ 拒识/歧义/错误
  分布报告 + 全量非回归 + closeout 报告（八条完成标志矩阵）。
- 真实样本未获授权 → 技术阶段可收，**M6 Final Acceptance 不宣称**（Gate 原文边界 + Q4 裁定）。

## 10. Gate 裁定记录（2026-10-09，D1 复核：CHANGES REQUIRED → FINAL FIX GO）

**五项修复项及落实位置**：

| 项 | 内容 | 落实 |
|---|---|---|
| D87-1 | source/target 工程文档身份解耦（双身份冻结、staleness 只钉 source、同源十候选顺序应用负例、source 删除/快照不可用拒绝行为） | §3 双身份定义、§4.1、§5.2 双列、§6.2-N5/N13 |
| D87-2 | Bearer token ≠ 个人审阅者身份；四概念分离（authentication evidence / reviewer attribution / human decision evidence / identity assurance），如实标 `declared` | §4.3、§9-D3 |
| D87-3 | v17 DDL 不变量闭合：apply_records 改定性为 CAS 约束生命周期账本；身份链同事务交叉校验 + 哈希绑定 + 负例；账本与工程提交同事务一致 | §5.1/§5.2 L1–L4、§6.2-N14 |
| D87-4 | architecture.md 全平台过度概括修正 | `docs/architecture.md` 现状对账（改为「M6 P&ID 写复用 apply-v2；全平台写入均受各自已批准的 domain transaction/governance executor 约束」） |
| D87-5 | `m6reg_` 随 D2 登记（身份格式 + 契约测试），`m6apply_` 留 D4 | §5.2 末条、§9-D2 |

**Q1–Q4 裁定**：

| 问题 | 裁定 |
|---|---|
| Q1 v17 两表 | 两表最小化方向**原则批准**；DDL FINAL HOLD→D87-1/3 闭合后随 D4 CODE GO 冻结；无证据要求第三张表 |
| Q2 令牌收窄节奏 | **APPROVED**：D2 零解禁；D3 按实际已登记表层逐词收窄；D4 处理余下真实需要者；禁止清单无独立 apply 字面令牌，不得假设需删除 |
| Q3 D2 只接 deterministic_rule_engine | **APPROVED**：不接 TypeSafe/LLM producer，保留接口但不建立伪造的 confidence calibration |
| Q4 真实样本路径 | **指定 Owner 授权真实 DXF 为正式资格化首选**；D2–D4 仅用合成或明确获准的开发材料；既有 DWG 历史实验结果≠未来使用授权；D5 真实资格化保持阻塞 |

**Gate 对既有 CAD 导入不一致的处置**：`docs/cad-import.md` 二进制 DXF 描述与代码不一致列为
**P1 documentation correctness debt**，下次正常文档维护独立修正；D2 只按已核实支持的 DXF 子集设计，
不因文档措辞推断能力（§2.4 登记项）。文字旋转与图元计数口径差异保持已知限制，
进入 gold corpus 的能力/歧义记录。

---

治理声明：本设计只覆盖 M6-2B（Charter §51 恢复建设）。WS3B / M9 Closeout / M10 FINAL ACCEPTANCE
维持 Owner 挂起；M10 Technical Completion、M11、M12、M13 不重开；DEPLOY 未授权；本文件不构成其收口。
