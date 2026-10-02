# M12-D1 — Project Graph / Cross-Domain Contract Design

> 状态：FINAL-FINAL DOC FIX（F77-1/F77-2 修订版，docs-only）。回答 Gate 冻结的六问并吸收
> M12-D1 Design Gate 裁定 R77 与 F77；所有结论在 final Design Gate 签署前均为 proposal。
> base = main@2cf198e0（M11 closeout 后）。Charter 部分（1.0.0→1.1.0）已获 CONTENT PASS。
> 依据 PROJECT_CHARTER v1.1.0 §55B 与 §57 Revision Proposal Record。

## 0. 范围与硬约束（复述冻结口径）

- 本阶段：docs-only。禁改 backend/**、frontend/**、schema/migration、runtime ports、audit/hash、P&ID/Cable payload、REST/MCP surface、测试实现。
- 代码顺序（D1 PASS 后固定）：D2 project identity/membership → D3 cross-domain links + governed mutation → D4 project validator/readiness → D5 deterministic package + read surface/UI + frozen e2e + closeout。
- 禁项：第三 domain / Domain SDK / plugin discovery / auth redesign / 新 approval-release 状态 / audit hash formation 变更。
- 依赖：M11 technical completion（已签）；不依赖 WS3B / M9 Closeout；不重开 M10。

## 1. Q1 — Project identity / membership：新持久化实体，不复用 project_settings

**事实**：现有 `project_settings` 是单行表（`name` + `metadata` dict，project_io.ProjectSettings），
没有稳定 project_id，没有成员索引；`audit_records.project_id` 列已存在但无生产写入方。
documents_registry（v14）有 `{document_id, domain, created_at}`，无 project 归属。

**Proposal（v15 新增两张表，不动 project_settings）**：

```
projects (
    project_id   TEXT PRIMARY KEY,          -- 'proj_' + uuid4 hex[:12]
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    archived_at  TEXT NOT NULL DEFAULT ''   -- 软归档；M12 内无删除项目操作
)

project_documents (
    project_id    TEXT NOT NULL REFERENCES projects(project_id),
    document_id   TEXT NOT NULL UNIQUE REFERENCES documents_registry(document_id),
    added_at      TEXT NOT NULL,
    added_by      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, document_id)
)
```

R77-Q1 修订（已吸收）：

- **project_documents 不存 domain**：成员 domain 一律查询时从 documents_registry 得出，
  根除 `{document_id=pid, domain=cable}` 假身份漂移面（两表 domain 无约束可不一致）。
- **document 单项目归属**：`document_id UNIQUE`——一个文档同一时刻只属于一个 active
  project；跨项目共享 visit 未来 milestone，M12 不开放。
- 稳定 project_id 由 `projects` 行提供；成员是"当前归属"，pinned revision 属于 link 与
  package（Q2/Q5/Q6）。
- 迁移 backfill：v15 建表后 seed 恰好一行默认项目（name 取 project_settings.name），
  registry 全量 OR IGNORE 进默认项目的 project_documents——存量行为零变化。
- `project_settings` 保持原语义（产品偏好），不与 projects 合并。

## 2. Q2 — EngineeringLink canonical schema（冻结 proposal）

```
engineering_links (
    link_id            TEXT PRIMARY KEY,     -- 'lnk_' + uuid4 hex[:12]
    project_id         TEXT NOT NULL REFERENCES projects(project_id),
    relation_type      TEXT NOT NULL
        CHECK(relation_type IN ('cable_endpoint_equipment')),
    source_domain      TEXT NOT NULL CHECK(source_domain IN ('pid','cable')),
    source_document_id TEXT NOT NULL REFERENCES documents_registry(document_id),
    source_object_ref  TEXT NOT NULL,        -- cable: segment_id
    source_endpoint    TEXT NOT NULL,        -- cable: 'from' | 'to'
    target_domain      TEXT NOT NULL CHECK(target_domain IN ('pid','cable')),
    target_document_id TEXT NOT NULL REFERENCES documents_registry(document_id),
    target_object_ref  TEXT NOT NULL,        -- pid: element id
    pinned_source_revision INTEGER NOT NULL CHECK(pinned_source_revision >= 0),
    pinned_target_revision INTEGER NOT NULL CHECK(pinned_target_revision >= 0),
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    deleted_at         TEXT NOT NULL DEFAULT '',
    deleted_by         TEXT NOT NULL DEFAULT ''
)
```

契约要点：

- **domain-neutral**：两端都是 {domain, document_id, object_ref} + pinned revision；
  link 表自身不理解 pid/cable 语义，`relation_type` CHECK 集是扩展点（M12 只冻一种）。
- **方向性与 revision binding**：创建/re-pin 时 pin 必须等于两端当前 revision；
  任何一端 revision 前进后 link 不自动跟随，validator 报 `stale_pinned_revision_*`（Q5），
  由人/agent 显式 re-pin（governed mutation）。pinned revision 允许 0（新建 P&ID 文档
  从 r0 开始，R78 追认：CHECK >= 0；undo 恢复旧内容仍保持 revision 单调前进）。

**R77-Q2 冻结的 relation invariants（cable_endpoint_equipment，写路径 fail-closed）**：

1. source 必须 domain='cable'，target 必须 domain='pid'（写库前校验，不依赖 CHECK 表达跨列约束）；
2. source_endpoint ∈ {'from','to'}；
3. 两端文档都必须在 link.project_id 的 project_documents 成员表内（跨项目 link 拒绝）；
4. create / re-pin 时 pinned_*_revision == 两端文档当前 revision；
5. **同一 active cable endpoint（segment_id + endpoint）该关系类型最多一个 link**——
   同一端同时连两台设备在物理上不可能，DB 层用部分唯一索引强制：
   `CREATE UNIQUE INDEX ... ON engineering_links(relation_type, source_document_id, source_object_ref, source_endpoint) WHERE deleted_at = ''`；
6. **M12 完全不开放 hard delete**：删除只有软删除（deleted_at/deleted_by，进 audit chain）；
   原稿"package 引用后 cleanup 硬删除"的说法删除——当前没有 package registry 能支撑该条件。

- **不得靠名字关联**：创建 link 必须给出两端确定性 object_ref；validator 只做存在性/域/
  pinned-revision 检查，不做名称匹配推断。

## 3. Q3 — 第一种真实关系：`cable_endpoint_equipment`

**精确语义**：一条 cable 段（CableSegment.id）的**指定端点**（from 或 to）在工程上端接于
某台 P&ID 设备元件（element id）。即「这根电缆的这一头接在这台设备上」。

- source = cable {document_id, segment_id, endpoint∈{from,to}}；target = pid {document_id, element_id}。
- CableSegment 现有 `from_node/to_node` 是自由文本节点名，M12 **不改 cable payload**；
  link 是独立工程对象，显式声明，不从节点名推断。

**R77-Q3 吸收——equipment predicate 在本设计冻结（D3 CODE GO 之前），不推迟到 D4**：

- 目标元件必须是 **equipment-eligible**：其 symbol category ≠ `INSTRUMENT_SYMBOL_CATEGORY`
  （`"仪表"`，backend/agentcad/m7_layout_contract.py:1240）。该规则复用仓库既有分类先例
  `EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST = True`（m7_layout_contract.py:1241，
  `_entity_kinds()` 同款语义：非仪表类目即 equipment 放置类）。
- **D3 建 link 时 fail-closed 判定**：target 元件不存在、不是 equipment-eligible、或
  endpoint/segment 不合法 → 稳定错误码拒绝写入，绝不允许写入语义为假的 equipment link。
- 校验只针对 pinned revision 下的对象存在性；名称一致性不进 M12 冻结面。

## 4. Q4 — Schema v15：需要；migration-safe 方案（R77-Q4 修订版）

**结论**：需要 v15（Q1/Q2 三张表：projects、project_documents、engineering_links）。
`CURRENT_SCHEMA_VERSION` 14→15，走既有 `_MIGRATIONS` 注册链（`_migration_15(connection)`）。

**v14→v15 upgrade（冻结步骤，含 R78 FK-ON 修订）**：

1. **FK 保持 ON**（R78 修订，Gate 已批）：现有 `_migrate()` 只在跨过 v14 时挂起 FK；
   v14→v15 只是新增表 + backfill，没有 v14 那种重建已有 FK 表的需求，因此迁移在
   FK  enforcement 开启下执行，更严格并与收尾的 `foreign_key_check` 相容。其余事务
   纪律照抄 v14 先例：`BEGIN EXCLUSIVE` 单事务内建三表 + backfill +
   `PRAGMA user_version=15`；任何非 SQLite 失败显式 rollback（R73-3 语义）。
2. backfill：seed 一行默认 projects（name 取 project_settings.name，project_id 确定性
   常量 `proj_m12default` + 迁移日志行）；registry 全量 OR IGNORE 进默认项目的
   project_documents。
3. **upgrade fixture**：既有 v14 fixture → migrate → 断言表结构、默认项目行、成员行数
   == registry 行数、user_version==15；再跑 v14 既有测试集证明存量行为零变化。
4. **backup/restore（R77-Q4 修订 + R78-1 强化）**：升级前用 **v14 binary/tool** 生成并校验 v14
   .pidbak（不得用 v15 binary 对 v14 库调 create_backup()——它会先 initialize_database()
   触发 _migrate() 把库迁到 v15 再备份，备份语义被污染）。部署 v15 binary 后执行
   v14→v15 migration。**restore 该 backup 首先得到 v14 库，随后由 v15 binary 再迁至
   v15**；不是"restore 后直接是 v15"。实现载体：`restore_backup(...,
   allow_pre_current_schema=True)`（keyword-only，默认 False 原路径不变）——legacy 分支
   必须取实际 schema version 并要求 `actual == metadata.schema_version` 且
   `< CURRENT_SCHEMA_VERSION`，在 quick/FK 之外调用 `_validate_required_schema()`
   （按 user_version 分层的 required 检查），instance-id 检查保持（R78-1 冻结）。
5. **forward rollback**：forward-only。回滚 = v14 backup restore（文档化步骤），不做
   v15→v14 逆迁移。降级检测沿用 `_migrate` 现有限制（newer version 拒开），语义不变。

**R77-Q4 吸收——D2 激活范围**：`_migration_15` 一次性创建三张表（links 表在 D2 只是
schema foundation）；**D2 只激活 project identity/membership 行为，engineering_links
在 D2 不开放任何 link service/API**，link 写路径在 D3 才启用。

**不改**：audit hash formation、ordinal 语义、audit_records 表结构（project_id 列已存在，
D3 起作为 link 治理事件的归属列首次生产使用——用列不是改结构）。

## 5. Q5 — Project validator（R77-Q5 修订版）

**成员 readiness 唯一来源（冻结，含 F77-1 waiver/profile/time 契约）**：

- P&ID：`assess_document_release_readiness(service, id, load_profile(), now=evaluation_as_of)`
  （release_validator.py:140）——仓库既有 canonical 函数，**唯一** P&ID readiness 来源，
  不另建错误计数通道。**profile 必须服务器侧 `load_profile()`，禁止请求方提交/覆盖**
  （deployment profile 可覆盖 built-in 是现有行为，服务器侧取效后即为 effective profile）。
- Cable：`assess_cable_document()`（D3 既有）。
- **evaluation_as_of**：显式、timezone-aware 的时间输入，是 validation 语义输入，
  **不是 created_at**（waiver 会过期，不指定 as_of 时同一请求不同时刻可返回不同
  verdict；冻结后语义确定）。
- 两端都针对文档**当前** revision 评估。

**跨域检查集（stable issue codes）**：

| issue_code | 触发条件 | severity |
|---|---|---|
| `dangling_link_source` | link source 文档不在项目成员表 | blocker |
| `dangling_link_target` | link target 文档不在项目成员表 | blocker |
| `missing_source_object` | cable 当前 revision 下 segment_id/endpoint 不存在 | blocker |
| `missing_target_object` | pid 当前 revision 下 element id 不存在 | blocker |
| `wrong_domain_reference` | link 端声明 domain 与 registry 实际 domain 不符 | blocker |
| `stale_pinned_revision_source` | source 当前 revision != pinned | warning（fail-on-warning） |
| `stale_pinned_revision_target` | target 当前 revision != pinned | warning（fail-on-warning） |

**R77-Q5 吸收（全部冻结）**：

- **先查 revision 再查对象**：stale 判定先做；一旦 stale，只报 stale finding，**不得声称
  检查了不存在的历史 pinned revision 对象**（当前 revision 下的对象检查让位）。
- **fail-on-warning**：参照 cable 既有先例（`document_non_empty` 就是 warning +
  also_fail_on_warning），两个 stale finding 列入 project profile 的 fail-on-warning 集——
  stale 使 project not_eligible，显式 re-pin 后恢复。
- **project eligible 精确定义**：所有 active member 的 readiness 都 eligible，**且**
  project-level failing rule 数为 0（blocker 直接失败；fail-on-warning 的 warning 同样
  失败），才 eligible；否则 not_eligible。绝不产生 Approved/Released。
- **soft-deleted links 不参与当前 readiness**（不查、不计、不进 package）。
- 全部检查纯函数、确定性排序（按 link_id 字典序），无时钟、无遍历序依赖。
- **hash/provenance（F77-1 扩充冻结）**：`result_hash` = sha256(canonical JSON(**至少**绑定：
  `evaluation_as_of`；每个 P&ID 成员的 readiness_hash / rule-bundle fingerprint；
  每个 Cable 成员的 result_hash / profile fingerprint；project-level issues；
  各 active link pinned 对))；canonical JSON 复用 cable_export `_canonical`
  先例；`profile_id='project-built-in'`、`profile_version=1`、`profile_fingerprint`
  （规则集自身 hash）。profile/waiver 或 evaluation_as_of 改变导致成员 readiness 改变时，
  project readiness provenance/hash **必须随之改变**。验证器只读、零副作用、零 audit 事件。

## 6. Q6 — Deterministic Project Delivery Package（R77-Q6 修订版）

ZIP（`ZIP_STORED` + 固定 ZipInfo(1980-01-01) + canonical JSON，照抄 cable_export 先例），
成员清单（冻结，顺序即 MANIFEST 行序）：

| # | member | 内容 |
|---|---|---|
| 1 | `MANIFEST.json` | {schema, project_id, package_schema_version, members: [{path, sha256, bytes}], links_sha256, readiness_sha256} |
| 2 | `project.json` | {project_id, name, members: [{domain, document_id, pinned_revision, content_hash}]} |
| 3 | `links/engineering_links.json` | canonical **active** links 全量（软删除历史不进包，由 audit chain 承担） |
| 4 | `readiness/project_readiness.json` | Q5 完整结果（含 result_hash/profile 三元组） |
| 5 | `domains/pid/<document_id>-r<rev>.json` | 该 revision 的 P&ID 文档确定性导出（复用既有 P&ID JSON 导出纯函数） |
| 6 | `domains/cable/<document_id>-r<rev>.zip` | 该 revision 的 cable 导出包（复用 D3 `export_cable_document` 字节） |

**R77-Q6 吸收（四点全改）+ F77-1 package 输入修订**：

1. **MANIFEST self-exclusion**：members 只列其余成员，MANIFEST.json 不给自身算 hash；
   MANIFEST 的完整性由 links_sha256/readiness_sha256 与成员 hash 集合共同锚定。
2. **删除 `created` 字段**；**package 输入冻结为 {project_id, member pins,
   evaluation_as_of}**——evaluation_as_of 是可复现验证必需的 semantic timestamp
   （Q5 F77-1），与已删除的 created_at 性质不同；包内除此语义输入外零时间戳。
   **fresh-process byte parity 定义**：相同 pins + 相同 evaluation_as_of +
   相同 effective server profile → byte-for-byte 相等。
3. **pins == 当前 revision（硬前置，F77-2 修订表述）**：所有 requested member pins 必须
   等于各文档当前 revision，且所有 active link pins == package member pins；任一不满足
   → stable error、不产包。**M12 package builder 只支持当前 exact revision，不支持从
   live document 重建任意历史 revision package；M12 不允许通过 revision-number rollback
   模拟历史导出**（现有 undo() 恢复旧内容也保持 revision 单调前进，revision number 永不
   降回）。历史 artifact 若需长期重取，依赖此前已持久化的 artifact/evidence，或未来单独
   设计的 immutable revision snapshot 能力——**不属于 M12**。
4. **package GET/download 只读、零 audit**（与 §55B「只读 API/UI」一致）；原稿"下载写
   audit"删除。正式的 issue/release package 若未来需要审计，另走 governed POST milestone。

- verify 纯函数、只读：重算每成员 sha256 与 MANIFEST 比对 + ZIP metadata（时间戳/压缩
  方式/extra）合规检查；单字节篡改必拒。

## 7. 测试矩阵（R77-§7 修订版，请 final Design Gate 一并冻结）

1. backend 单测：
   - v15 migration：fixture 升级、backfill、rollback、user_version；**v14 binary 备份 →
     v15 migrate → restore 得 v14 → v15 binary 再迁 v15** 全链；
   - membership：domain 从 registry 得出（成员表无 domain 列）、document 单项目归属
     UNIQUE、跨项目 link 拒绝；
   - link 治理：create/re-pin 的 relation invariants（orientation/endpoint/成员归属/
     pin==当前 revision）、active endpoint 唯一（部分唯一索引）、软删除进 audit 边界、
     equipment predicate fail-closed（仪表类 target 拒绝）；
   - validator：七 issue code 正负例、stale 只报 stale、soft-deleted 不参与、
     canonical P&ID/Cable readiness 聚合（assess_document_release_readiness 唯一来源）；
   - package：同输入双构建字节相等（fresh process）、MANIFEST self-exclusion、
     missing/extra/duplicate member 拒、ZIP metadata 突变拒、单字节 tamper 拒、
     pins != 当前 revision 稳定错误不产包。
2. e2e（frozen，真实后端）：建项目→加成员→建 link→validator readiness eligible→
   pin 前进触发 stale→project not_eligible 且 package build 稳定错误→显式 re-pin→
   恢复 eligible→导出 package→HTTP body==UI download==fresh-process 二次构建字节。
3. shared-mode：匿名 project/link/package 三族 GET 全 401；带 token UI 只读 surface 可用。
4. 非回归：全量既有测试零删改零放宽（M5 gate / backend / frontend / browser / shared）。
5. F77 parity：固定 evaluation_as_of + 固定 effective server profile，跨 fresh process
   project readiness / package bytes 完全一致。
6. F77 provenance drift：profile/waiver 或 evaluation_as_of 改变导致 P&ID member
   readiness 改变时，project readiness provenance/hash 必须随之改变；另断言全库不存在
   任何"revision number 回退后重建历史包"的路径。

**性能预算（R77-§7 增补，冻结）**：以 main@2cf198e0、同 Python/同机器、后端全量测试
命令的 wall-clock 记录 pre-M12 基线；M12 closeout 的增量预算 **≤ +10%**；超出须单独
Gate amendment，不得静默接受。

## 8. 请求签署

请 Gate 按 R77 逐项复核 Q1–Q6 与 §7 修订。签署后合并 PR #77（Charter 1.1.0 落地），
然后持 M12-D2 CODE GO 开工 project identity/membership（D2 不开放 link service/API）。
