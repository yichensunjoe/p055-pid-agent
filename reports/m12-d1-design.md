# M12-D1 — Project Graph / Cross-Domain Contract Design

> 状态：DESIGN PREP（docs-only，M12-D1 DESIGN PREP GO 已批）。本文回答 Gate 冻结的六问；
> 所有结论在 D1 Design Gate 签署前均为 proposal。base = main@2cf198e0（M11 closeout 后）。
> 依据 PROJECT_CHARTER v1.1.0 §55B（本 PR 同批提交）与 §57 Revision Proposal Record。

## 0. 范围与硬约束（复述冻结口径）

- 本 PR 与本文档阶段：docs-only。禁改 backend/**、frontend/**、schema/migration、runtime ports、audit/hash、P&ID/Cable payload、REST/MCP surface、测试实现。
- 代码顺序（D1 PASS 后固定）：D2 project identity/membership → D3 cross-domain links + governed mutation → D4 project validator/readiness → D5 deterministic package + read surface/UI + frozen e2e + closeout。
- 禁项：第三 domain / Domain SDK / plugin discovery / auth redesign / 新 approval-release 状态 / audit hash formation 变更。
- 依赖：M11 technical completion（已签）；不依赖 WS3B / M9 Closeout；不重开 M10。

## 1. Q1 — Project identity / membership：新持久化实体，不复用 project_settings

**事实**：现有 `project_settings` 是单行表（`name` + `metadata` dict，project_io.ProjectSettings），
没有稳定 project_id，没有成员索引；`audit_records.project_id` 列已存在但无生产写入方
（grep 证实仅有 DDL）。documents_registry（v14）有 `{document_id, domain, created_at}`，
无 project 归属。

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
    domain        TEXT NOT NULL CHECK(domain IN ('pid','cable')),
    document_id   TEXT NOT NULL REFERENCES documents_registry(document_id),
    added_at      TEXT NOT NULL,
    added_by      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, document_id)
)
```

- 稳定 project_id 由 `projects` 行提供；成员身份 = {domain, document_id}，revision 不进成员表
  （成员是"当前归属"，pinned revision 属于 link 与 package，见 Q2/Q5/Q6）。
- 迁移 backfill：v15 建表后 seed 恰好一行默认项目（project_id 确定性生成并写入 migration
  日志行；name 取 project_settings.name），全部现有 documents_registry 行 OR IGNORE 进
  `project_documents`——存量行为零变化，老库升级后「有一个包含全部文档的默认项目」。
- `project_settings` 保持原语义（UI 显示名 + 自由 metadata），不与 projects 合并：
  一个是产品偏好，一个是工程治理对象，生命周期不同。

## 2. Q2 — EngineeringLink canonical schema（冻结 proposal）

```
engineering_links (
    link_id            TEXT PRIMARY KEY,     -- 'lnk_' + uuid4 hex[:12]
    project_id         TEXT NOT NULL REFERENCES projects(project_id),
    relation_type      TEXT NOT NULL
        CHECK(relation_type IN ('cable_endpoint_equipment')),
    source_domain      TEXT NOT NULL CHECK(source_domain IN ('pid','cable')),
    source_document_id TEXT NOT NULL REFERENCES documents_registry(document_id),
    source_object_ref  TEXT NOT NULL,        -- cable: segment_id；pid: element id
    source_endpoint    TEXT NOT NULL DEFAULT '',  -- cable: 'from'|'to'；pid: ''
    target_domain      TEXT NOT NULL CHECK(target_domain IN ('pid','cable')),
    target_document_id TEXT NOT NULL REFERENCES documents_registry(document_id),
    target_object_ref  TEXT NOT NULL,        -- pid: element id
    pinned_source_revision INTEGER NOT NULL CHECK(pinned_source_revision >= 1),
    pinned_target_revision INTEGER NOT NULL CHECK(pinned_target_revision >= 1),
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    deleted_at         TEXT NOT NULL DEFAULT '',
    deleted_by         TEXT NOT NULL DEFAULT ''
)
```

契约要点：

- **domain-neutral**：两端都是 {domain, document_id, object_ref} 三元组 + pinned revision；
  link 表自身不理解 pid/cable 语义，`relation_type` 的 CHECK 集是扩展点（M12 只冻一种）。
- **方向性**：source→target 有工程语义（见 Q3），不 symmetric、不反向冗余存储。
- **revision binding**：创建时钉死两端 revision；任何一端文档 revision 前进后 link 不自动跟随，
  由 project validator 报 `stale_pinned_revision_*`（Q5），由人/agent 显式 re-pin（ governed
  mutation，走 D3 的 update surface）。
- **deletion semantics**：软删除（`deleted_at/deleted_by`）。硬删除只允许在 link 未被任何
  release evidence package / delivery package 引用时由 cleanup 路径执行（M12 内不开放）；
  删除与 re-pin 同权治理：进 audit hash chain（Q4）。
- **不得靠名字关联**：创建 link 必须给出两端确定性 object_ref（cable segment_id / pid
  element id）；validator 只做存在性/域/pinned-revision 检查，不做任何名称匹配推断
  （名称一致性至多作为 warning 级附加检查，且 D4 再定，不进 M12 冻结面）。

## 3. Q3 — 第一种真实 P&ID↔Cable 关系：`cable_endpoint_equipment`

**精确语义**：一条 cable 段（CableSegment.id）的**指定端点**（from 或 to）在工程上端接于
某台 P&ID 设备元件（element id，如泵/马达/ mcc 类设备）。即「这根电缆的这一头接在这台
设备上」—— Cable schematic 与 P&ID 之间最真实、最可验证的工程关系。

- source = cable {document_id, segment_id, endpoint∈{from,to}}；target = pid {document_id, element_id}。
- CableSegment 现有 `from_node/to_node` 是自由文本节点名（如 PMP-101），M12 **不改 cable
  payload**；link 是独立工程对象，建立时由人/agent 显式声明，不从节点名推断。
- 为什么是它：① 两端对象都在各自域内已有稳定身份；② 校验完全确定性（段存在、端点合法、
  元件存在于 pinned revision、元件类型属设备类）；③ 直接服务 completion criterion 2
  （"真实工程关系"），且是 delivery package 里跨域一致性的最小可验证单元。
- 设备类判定（D4 细节）：白名单 element type 集合，D4 设计时从现有 symbol registry 枚举冻结，
  不在本设计展开。

## 4. Q4 — Schema v15：需要；migration-safe 方案冻结 proposal

**结论**：需要 v15（Q1/Q2 两张表 + project_documents）。`CURRENT_SCHEMA_VERSION` 14→15，
走既有 `_MIGRATIONS` 注册链（`_migration_15(connection)`）。

**v14→v15 upgrade（冻结步骤）**：

1. 沿用 v14 先例：跨版本时 `PRAGMA foreign_keys=OFF`（BEGIN 之前切换，事务结束后恢复），
   `BEGIN EXCLUSIVE` 单事务内建两表 + backfill + `PRAGMA user_version=15`；
   任何非 SQLite 失败显式 rollback（照抄 _migrate 的 R73-3 语义）。
2. backfill：seed 一行默认 projects（name 取 project_settings.name，project_id 确定性
   `proj_m12default` 常量 + 迁移日志行记录）；`INSERT OR IGNORE SELECT ... FROM documents_registry`
   全量进默认项目的 project_documents（domain 直取 registry.domain）。
3. **upgrade fixture**：测试用既有 v14 fixture（已有 v14 库构造先例）→ migrate → 断言
   表结构、默认项目行、成员行数 == registry 行数、user_version==15；再跑 v14 既有测试集
   证明存量行为零变化。
4. **backup/restore**：迁移前强制走既有备份机制（.pidbak 先例，文件名带版本与时间戳）；
   restore 路径用既有 recovery 流程，v15 库 restore 后 user_version=15 可再打开。
5. **forward rollback**：forward-only。回滚 = backup restore（文档化步骤），不做 v15→v14
   逆迁移。降级检测沿用 `_migrate` 现有限制（newer version 直接拒开），语义不变。

**不改**：audit hash formation、ordinal 语义、audit_records 表结构（project_id 列已存在，
D3 起作为 link 治理事件的归属列写入——这是列的首次生产使用，不是结构变更）。

## 5. Q5 — Project validator：stable issue codes / readiness / hash-provenance

**聚合结构**：project readiness = 成员文档 readiness ∪ 跨域检查。成员文档侧复用既有
单域 readiness 源（cable：`assess_cable_document`；pid：validation/project_index 的错误计数
通道——D4 冻结具体取数函数）；跨域检查集（stable issue codes，冻结）：

| issue_code | 触发条件 | severity |
|---|---|---|
| `dangling_link_source` | link source 文档不在项目成员表 | blocker |
| `dangling_link_target` | link target 文档不在项目成员表 | blocker |
| `missing_source_object` | cable 文档 pinned revision 下 segment_id/endpoint 不存在 | blocker |
| `missing_target_object` | pid 文档 pinned revision 下 element id 不存在 | blocker |
| `wrong_domain_reference` | link 端声明 domain 与 registry 实际 domain 不符 | blocker |
| `stale_pinned_revision_source` | source 当前 revision != pinned | warning |
| `stale_pinned_revision_target` | target 当前 revision != pinned | warning |

- 全部检查纯函数、确定性排序（按 link_id 字典序），无时钟、无遍历序依赖。
- **readiness 只能是 `eligible` / `not_eligible`**：任一 blocker → not_eligible；warning 不降级
  （与 D3 cable readiness 的 blocker/warning 语义对齐）。绝不产生 Approved/Released。
- **hash/provenance**：project readiness 结果含 `result_hash` = sha256(canonical JSON(issue
  列表 + 各成员 content_hash + 各 link pinned 对))，canonical JSON 复用 cable_export 的
  `_canonical` 先例（排序键、无空白）；另带 `profile_id='project-built-in'`、
  `profile_version=1`、`profile_fingerprint`（规则集自身 hash，规则集变更即变）。验证器只读，
  零副作用、零 audit 事件（与 D3 cable surface 同纪律）。

## 6. Q6 — Deterministic Project Delivery Package：成员清单冻结 proposal

ZIP（`ZIP_STORED` + 固定 ZipInfo(1980-01-01) + canonical JSON，全部照抄 cable_export
确定性先例），成员清单（冻结，顺序即 MANIFEST 行序）：

| # | member | 内容 |
|---|---|---|
| 1 | `MANIFEST.json` | {schema, project_id, package_schema_version, created 不含时钟（由调用方传入或省略）, members: [{path, sha256, bytes}], links_sha256, readiness_sha256} |
| 2 | `project.json` | {project_id, name, members: [{domain, document_id, pinned_revision, content_hash}]} |
| 3 | `links/engineering_links.json` | canonical 全量 link（含软删除标记） |
| 4 | `readiness/project_readiness.json` | Q5 完整结果（含 result_hash/profile 三元组） |
| 5 | `domains/pid/<document_id>-r<rev>.json` | 该 pinned revision 的 P&ID 文档确定性导出（复用既有 P&ID JSON 导出纯函数） |
| 6 | `domains/cable/<document_id>-r<rev>.zip` | 该 pinned revision 的 cable 导出包（复用 D3 `export_cable_document` 字节） |

- 输入 = {project_id, 成员 pinned revision 集合}；同输入跨 fresh process byte-for-byte 相等
  （e2e 双进程断言，D5）。
- 篡改检测：MANIFEST 每成员 sha256 + 顶层 verify 流程（重算成员 hash 与 MANIFEST 比对），
  verify 纯函数、只读。
- 导出动作走 audit（delivery package 下载是一次受治理动作，记录 event；不改 hash chain
  formation，只作为新 event_type 消费现有链）。

## 7. 测试矩阵 proposal（请 D1 Design Gate 一并冻结）

1. backend 单测：v15 migration（fixture 升级、backfill、rollback、backup/restore）、link
   治理（create/re-pin/delete 权限+audit 边界）、validator 七 issue code 正负例、package
   确定性（同输入两次构建字节相等）、verify 篡改拒绝（单字节突变必失败）。
2. e2e（frozen，真实后端）：建项目→加成员→建 link→validator readiness→pin 前进触发
   stale→导出 package→HTTP body==UI download==fresh-process 二次构建字节。
3. shared-mode：匿名 project/link/package 三族 GET 全 401；带 token UI 只读 surface 可用。
4. 非回归：全量既有测试零删改零放宽（M5 gate / backend / frontend / browser / shared）。

## 8. 请求签署

请 Gate 逐项裁：Q1–Q6 与 §7 测试矩阵。签署后进入 D2（project identity/membership 代码），
代码 PR 将只含 whitelist 内文件（store/migration、project service、api、tests），并附
upgrade fixture。
