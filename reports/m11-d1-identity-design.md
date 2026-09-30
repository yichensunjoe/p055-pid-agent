# M11-D1 详细设计：domain-neutral document identity boundary（送 Gate 审，docs-only）

> 前置：M11 Definition APPROVED/FROZEN @255f53d；M11-D1 DESIGN PREP GO 已签。本文档零产品代码。基线 main@3379477（schema v13）。

## 1. 文档身份模型：六类表的关系（目标态 v14）

```
documents_registry                # 新增：全局文档身份注册表（domain-neutral identity）
  document_id TEXT PK             # 全局唯一，ID 生成器带 domain 前缀（doc_*/cab_*）
  domain TEXT NOT NULL            # 'pid' | 'cable'（domain discriminator）
  payload_table TEXT NOT NULL     # 'documents' | 'cable_documents'（路由，应用层只读）
  revision INTEGER NOT NULL
  created_at/updated_at TEXT

documents                         # P&ID 载荷表（现有结构一行不动；由 registry 行指向）
cable_documents                   # 新增：Cable 载荷表（document_id PK 且 FK→registry，同库独立表）
cable_segments                    # 新增：Cable 段表（FK→cable_documents；D1 只建空表，D2 才用）

agent_sessions / agent_approvals / agent_tool_calls
  document_id TEXT NOT NULL FK → documents_registry(document_id)   # 由 FK→documents 改挂 registry

audit_records                     # 一行不动：无 FK、全局 ordinal hash chain，domain 无关
```

治理面（session/approval/tool-call）与审计面（audit_records）从此只认 registry 身份，不理解 domain 载荷；domain 语义只存在于各自载荷表与 adapter。

## 2. domain-neutral identity 方案比选（冻结候选 A）

| 方案 | 说明 | 判定 |
|---|---|---|
| **A. 全局注册表（generic document registry）** | 新增 registry 作唯一身份与 FK 目标；载荷表按 domain 分表，registry 行路由 | **选定** |
| B. 治理表去 FK + domain 判别列 | 去掉治理表 FK，加 domain 列 | 否决：SQLite 侧失去声明式引用完整性，且审计/治理 join 路径分叉 |
| C. 每 domain 复制一套治理表 | cable 自建 sessions/approvals 表 | 否决：治理面碎片化，runtime 单平面语义破裂，跨 domain 审计序无从谈起 |

A 方案的四个冻结点：
- **ID 全局唯一**：registry PK 是唯一权威；ID 生成器按 domain 前缀分配（现有 `doc_*` 不变，Cable 用 `cab_*`），但前缀只是可读性，真正的防碰撞是 PK 冲突即 IntegrityError → **fail-closed**。
- **domain discriminator**：registry.domain 列，写入时由对应 domain 的 store 路径设置，应用层不可跨 domain 改写。
- **现有 P&ID 行映射**：v14 迁移为每个 documents 行插入 registry 行（domain='pid', payload_table='documents', revision=现有 revision），一行不漏。
- **ID collision fail-closed**：任何 domain 的插入先撞 PK 即拒；跨 domain 复用同一 document_id 物理不可能。

## 3. 治理表迁移方案

三张治理表的 `document_id` 外键从 `documents(id)` 改挂 `documents_registry(document_id)`：
- SQLite 不能 ALTER FK → 采用标准 12 步表重建（create new → copy → drop → rename），外键引用对象改为 registry。
- **禁止 dummy P&ID document 占位**：Cable 文档从 registry 行直接成立，不借 P&ID 载荷。
- 迁移后治理表内既有的 P&ID document_id 全部在 registry 有对应行（迁移保证），引用完整性在迁移事务内逐表校验（COUNT 对账）。

## 4. M10 runtime contract 兼容性（冻结）

- HarnessStorePort / AgentSession / ToolApproval / ToolCallRecord 的外部语义**全部不变**；M10 已合并的行为锁（golden parity、五闭包、隔离锁）继续有效。
- store.py 的治理面方法实现改挂 registry join（读载荷 revision 时经 registry 路由），但方法签名与返回模型不变。
- 若 D1 实施中发现必须修改 runtime port 或 model contract → **显式新 Gate 项，不顺手改**（本设计未发现需要）。

## 5. 原子事务边界（冻结）

Cable governed write（D2 落地，D1 只定义边界）必须是 store 层单一 primitive（模式同 M9 的 commit_release）：**BEGIN IMMEDIATE → cable 载荷写 + registry revision 更新 + tool-call/approval/session 闭包 + audit append → COMMIT**，任一失败全回滚。同库同连接单事务，无跨库问题（独立库方案已被 DEF-3 排除）。

## 6. Audit hard lock（冻结）

audit_records 表结构、global ordinal 连续性、prev_hash/record_hash 形成算法、chain schema/version、genesis verification 验证逻辑——**全部不变**。D1 迁移只改治理表 FK 与新增表，不触碰 audit_records。若实施中发现任一项必须改 → **STOP / RETURN TO GATE**。

## 7. migration algorithm（v13 fixture → v14）

1. PRAGMA user_version 校验 =13；BEGIN IMMEDIATE。
2. CREATE documents_registry（空）。
3. CREATE cable_documents / cable_segments（空表，D1 不写入任何 Cable 数据）。
4. 12 步重建 agent_sessions → 新 schema（document_id FK→registry）；COPY 全量；DROP 旧表；RENAME。approvals、tool_calls 同法依次做。
5. INSERT registry 行：SELECT 自 documents（domain='pid'）。
6. 对账校验：治理表 distinct document_id ⊆ registry；三表行数迁移前后一致；registry 行数 == documents 行数。
7. PRAGMA user_version=14；COMMIT。任一步失败 → ROLLBACK，库保持 v13 可读可写。
- FK 策略：应用连接历史不开 foreign_keys pragma（以代码核验为准，设计稿记录实测结果）；重建后声明式 FK 指向 registry，开启 pragma 的客户端同样得到一致约束。
- 失败注入测试：在第 4–6 步各注入一次异常，断言 ROLLBACK 后 v13 全功能（读写/迁移重跑幂等）。
- 迁移重复执行：user_version=14 即跳过（幂等）。

## 8. compatibility evidence（D1 必须交付的测试）

- v13 P&ID 数据/文档历史/session/approval/tool-call/audit chain 迁移前后语义与引用关系不变（逐表 COUNT + 抽样字段 + audit verify_chain ok）。
- 全量 1706 baseline 测试在 v14 库上全绿（测试库自动迁移路径同生产）。
- Cable 数据不进 P&ID digest/projection（D1 阶段 Cable 表为空，测试以 registry discriminator 断言 P&ID 查询路径不触碰 cable_* 表）。
- registry 路由负测：用 cable document_id 走 P&ID 读路径 → 404/not_found（fail-closed）。

## 9. backup / rollback runbook

- 迁移前：create_backup（现有 database_recovery 备份格式）+ inspect_backup 验证；restore 演练用一次性 v13 库实测（备份→restore→verify_chain ok）。
- v14→v13 不是普通 git revert：代码回退后 v14 库不能被 v13 binary 打开（DatabaseVersionError 如实记录）；真正的降级 = 恢复迁移前 backup。是否提供专门 downgrade procedure：D1 **不提供**（数据面为空，无 downgrade 价值；若 D2 后需要，另开 Gate）。

## 10. D1 CODE PR 的 whitelist / 测试矩阵 / rollback boundary

**Whitelist**：database_recovery.py（v14 迁移 + registry/cable 建表 + required_tables）、store.py（治理面方法改挂 registry 路由 + registry 读写 + v14 对账校验）、tests（新增 test_m11_d1_identity.py、扩展 test_database_recovery.py）、docs。Cable validator/export/UI/runtime wiring/D2 内容**一律不在 D1**（D1 的 cable 表为空壳，证明身份边界与迁移，不证明 domain 功能——这正是 D1/D2 的证据分层）。

**测试矩阵**：① v13→v14 迁移 happy path（fixture 库）；② 三步失败注入回滚（§7）；③ 幂等重跑；④ 对账校验（治理表/registry/documents 行数与引用）；⑤ audit chain 迁移前后 verify_chain ok 且 ordinal 连续；⑥ registry 路由负测（fail-closed）；⑦ v14 上 1706 baseline 全绿；⑧ 隔离锁不回归（runtime 包仍零 P&ID 新增依赖——registry 属 store 层，不在 runtime 包）。

**Rollback boundary**：D1 纯代码 revert 可回（v14 空 Cable 数据，生产可留 v14）；已迁移的生产库回退代码 = 恢复迁移前 backup（runbook §9）。

**D1 不提前实现 D2 adapter 的理由**：身份边界未过 Gate 前，adapter 的持久化假设不可靠；且 D1 的 evidence（迁移+对账+负测）与 D2 的 evidence（governed write 全链）分层后，若 D2 暴露问题可归因于 wiring 而非身份边界设计。

## 11. 待 Gate 裁的决策点

1. 注册表方案 A 选定与否；2. cable 表空壳进 D1 是否认可（vs 连表也不建）；3. whitelist 是否需增删；4. downgrade「D1 不提供」是否认可。
