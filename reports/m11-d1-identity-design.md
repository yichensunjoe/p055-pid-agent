# M11-D1 详细设计 v2：domain-neutral document identity boundary（送 Gate 审，docs-only）

> 前置：M11 Definition FROZEN @255f53d；D1 v1 被裁 CHANGES REQUIRED（R11-D1-1~5），本版逐项闭合。基线 main@3379477（schema v13）。已实测核验：`_connect()` 与 `initialize_database()` 均 `PRAGMA foreign_keys=ON`（R11-D1-1 的「FK 不开」前提作废，本版按 FK 开启重写）。

## 1. 身份模型（目标态 v14，按 R11-D1-2 最小化）

```
documents_registry                      -- 唯一职责：identity，不是第二份 revision truth
  document_id TEXT PRIMARY KEY
  domain TEXT NOT NULL CHECK(domain IN ('pid','cable'))   -- 创建后不可改写
  created_at TEXT NOT NULL

documents                     -- P&ID 载荷（一行不动；revision 真相仍在载荷表 + DomainAdapter.document_context）
cable_documents               -- D1 建空壳（无 segment 数据模型；cable_segments 延后 D2）

agent_sessions / agent_approvals / agent_tool_calls
  document_id ... FOREIGN KEY(document_id) REFERENCES documents_registry(document_id) ON DELETE CASCADE
  -- 表内层级 FK（approvals.session_id→sessions CASCADE、tool_calls.session_id→sessions CASCADE、
     tool_calls.approval_id→approvals SET NULL）原样保留

audit_records                 -- 完全不动：无 FK、全局 ordinal hash chain
```

**R11-D1-2 冻结**：registry 只存 identity（document_id/domain/created_at）；**删除 revision 列与 payload_table 列**——runtime 的 revision 正式接口是 `DomainAdapter.document_context()`（M10 已冻结），载荷表是唯一 revision 真相；domain 到载荷表的路由由代码静态完成，不用库内字符串表名做动态路由；domain 一经创建不得改写。

## 2. v14 DDL（冻结，R11-D1-4 要求逐字给出）

**cable_documents 精确 DDL（R11-D1-6 冻结，D2 不得改变这些 envelope 字段语义；data_json 是 D2 Cable JSON contract 的 opaque payload carrier——D2 只定义 JSON 内容，不再 ALTER 表，无需 v15）：**

```sql
CREATE TABLE cable_documents (
    document_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(document_id)
        REFERENCES documents_registry(document_id)
        ON DELETE CASCADE
);
```

（Cable 的 revision 真相存于此 envelope 的 revision 列，与 P&ID documents.revision 同层——registry 不含 revision 的口径一致。）

三表仅 `document_id` 的 FK 目标从 `documents(id)` 改为 `documents_registry(document_id)`，`ON DELETE CASCADE` 逐字保留；其余列、默认值、层级 FK 全部不变。以 agent_tool_calls 为例（v14）：

```sql
CREATE TABLE agent_tool_calls_v14 (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    document_id TEXT NOT NULL,
    permission TEXT NOT NULL,
    risk TEXT NOT NULL,
    approval_id TEXT,
    intent_hash TEXT NOT NULL,
    base_revision INTEGER,
    result_revision INTEGER,
    status TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES documents_registry(document_id) ON DELETE CASCADE,
    FOREIGN KEY(approval_id) REFERENCES agent_approvals(id) ON DELETE SET NULL
);
```

（agent_sessions_v14 / agent_approvals_v14 同法：仅 document_id FK 目标改 registry；sessions 表层级不变，approvals 保留 session_id FK CASCADE。实现时以 `sqlite_master` 现读现迁：迁移第一步把三表与索引的当前 DDL 读入，**以现读 DDL 为唯一真源**生成 v14 建表语句，防手工抄错。）

**索引契约**（重建后逐条重建，名称与定义逐字保留）：
```sql
CREATE INDEX idx_agent_sessions_document_created ON agent_sessions(document_id, created_at DESC);
CREATE INDEX idx_agent_approvals_session_created ON agent_approvals(session_id, created_at ASC);
CREATE INDEX idx_agent_tool_calls_session_started ON agent_tool_calls(session_id, started_at ASC);
```

## 3. 迁移算法（冻结，R11-D1-1 真实可执行版）

选 **migration 专用 FK-OFF 路径**（三张治理表互相 FK 引用，FK-ON 下 12 步 rename  dance 不可能安全；`foreign_key_check` 在提交前兜底完整性）：

```
0. 校验 user_version==13；PRAGMA foreign_keys=OFF（须在 BEGIN 外设置，SQLite 限制）
1. BEGIN IMMEDIATE
2. 读入三表+索引当前 DDL（sqlite_master）作为生成真源
3. CREATE documents_registry（§1 最小 schema）
4. backfill registry：INSERT INTO documents_registry SELECT id,'pid',created_at FROM documents
5. CREATE cable_documents（空壳，D1 无写入路径）
6. 逐表 12 步重建（agent_sessions → agent_approvals → agent_tool_calls）：
   CREATE <t>_v14（§2 生成）→ INSERT SELECT * 全列拷贝 → COUNT 对账（旧表==新表）
   → DROP <t> → ALTER <t>_v14 RENAME TO <t> → 重建该表索引（§2 契约）
   （建表按父→子顺序，DROP/RENAME 按子→父顺序回退执行）
7. 全局对账：registry 行数==documents 行数；三表行数迁移前后一致；
   三表 distinct document_id ⊆ registry
8. PRAGMA foreign_key_check —— 必须为空，非空即 ROLLBACK
9. PRAGMA user_version=14
10. COMMIT
11. PRAGMA foreign_keys=ON（finally 保证；任何异常 → ROLLBACK + FK ON，库保持 v13 可读可写）
```

幂等：user_version==14 即整体跳过。失败注入测试点位：4 / 6 / 8 各注一次异常，断言回滚后 v13 全功能 + 重跑成功。

## 4. P&ID 生命周期不变量（R11-D1-3 冻结）

| 操作 | 事务边界 |
|---|---|
| 新建 P&ID 文档 | documents 行 + registry(domain='pid') 行同一事务 |
| project import | 全部导入 documents + 对应 registry 行同一事务 |
| 普通 revision update | 只写载荷表，**registry 不动**（无 revision 列） |
| 删除 P&ID 文档 | documents 删除 + registry identity 删除同一事务；治理表经 v14 FK `ON DELETE CASCADE` 行为与 v13 逐字一致；audit 行无 FK、保留（删除证据可审计，原语义不变） |
| 跨 domain ID 注册 | registry PK 冲突 → IntegrityError，原子 fail-closed |

store.py 的文档 create/import/delete 路径增加 registry 同事务写入；治理面方法读 revision 仍走 DomainAdapter/载荷表，registry 不参与读路径。

## 5. runtime contract / audit 硬锁（不变）

HarnessStorePort/AgentSession/approval/tool-call 语义不变；必须改 port/model → 显式新 Gate 项。audit_records 结构、global ordinal、hash 形成、chain schema、genesis 验证全部不动；必须改 → STOP RETURN TO GATE。原子闭包边界（D2 落地）同 §5 v1：单一 store primitive 同事务。

## 6. compatibility evidence（按 R11-D1-4 升级）

1. 三张重建表**全行全持久字段** pre/post 等价（非抽样：逐行 dict 比对）。
2. 索引契约存在（§2 三条 + sqlite_autoindex）。
3. 迁移后 `PRAGMA foreign_key_check` 为空。
4. 删除一个 P&ID document 后：治理表 cascade 行为与 v13 一致（同 fixture 双版本对比）、audit 行保留。
5. fresh P&ID create / import 后 registry 完整（每 documents 行恰一 registry 行）。
6. cross-domain ID collision：以 cable domain 注册已存在的 pid id → IntegrityError，且两表均无变化。
7. 1706 baseline 在 v14 库全绿（含 M5/quality）；隔离锁不回归。
8. 迁移前后 audit `verify_chain()` ok 且 ordinal 连续。

## 7. backup / rollback runbook（R11-D1-5 修正口径）

- **代码普通 revert 仅在迁移未执行时成立**。DB 已 v14 时回 v13：停服务 → 恢复 pre-v14 verified backup（create_backup/inspect_backup 格式）→ 用 v13 binary 验证 instance_id 与 audit chain（verify_chain ok）→ 启动。v14 库不能被 v13 binary 打开（DatabaseVersionError）如实记录。
- **不提供通用 v14→v13 downgrade migration**（已获批准）；backup→restore 演练是 D1 硬交付。

## 8. CODE GO 硬锁（Gate 预冻，实施必须遵守）

1. **FK-OFF 时序**：`PRAGMA foreign_keys=OFF` 必须发生在 `_migrate()` 外层事务开始之前——不能在 `_migration_14()` 内部才执行（SQLite 事务内改 FK pragma 无效）；实现需重构外层 migration orchestration 满足 v2 §3 步骤 0→1，初始化路径预开 FK 的事实保持。
2. **旧 upgrade path 不破坏**：D1 不得只让「exact v13 fixture」能升到 v14——现有全部 migration 测试语料（v1–v13 各版本 fixture）必须继续通过；v14 事务 COMMIT 前 `PRAGMA foreign_key_check` 必须为空。
3. **P&ID lifecycle 实现范围**：registry 同事务维护全部落在 store.py 内（create→store.save()、import→import_documents_atomic()、delete→store.delete() 三个挂点），不扩到 API/runtime。

## 9. D1 CODE whitelist / 分层 / 决策点复核

Whitelist：database_recovery.py（v14 迁移 §3 + registry/cable 建表 + required_tables）、store.py（文档 create/import/delete 加 registry 同事务 + registry 读写）、tests（test_m11_d1_identity.py 新 + test_database_recovery.py 扩展）、docs。cable 空壳进 D1、cable_segments 延后 D2（按你建议采纳）；**禁**：runtime ports/models、audit hash、main composition、validator/export/UI/API、CABLE 数据写入路径。

§11 v1 四决策点现状：方案 A 收敛为 identity-only registry（R11-D1-2 已落）；cable 空壳=部分批准（segments 延后）；whitelist 按上；downgrade 不提供（backup/restore 演练硬要求）。

请裁 M11-D1 Design v2；PASS 后请签 M11-D1 CODE GO。
