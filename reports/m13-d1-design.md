# M13-D1 — Governed Multi-Domain Change Sets: Contract / Impact / Preview / Atomic Commit Design

> 状态：DESIGN PREP（docs-only，M13-D1 DESIGN PREP GO）。回答 Gate 冻结的问题；签署前均为
> proposal。base = main@b9e125f（M12 implementation closeout COMPLETE）。
> 依据 PROJECT_CHARTER v1.2.0 §55C（本 PR 同批提交）与 §57 Revision Proposal Record。

## 0. 依赖与硬约束（复述冻结口径）

- 依赖 M12 closeout；继承 M10 runtime/permission/approval/audit；复用 M11 双真域、M12
  project graph / readiness / deterministic package。不依赖 WS3B/M9 Closeout/M10 FINAL；
  不重开 M10/M11/M12 sealed 状态。
- 禁项（D1 及全程）：第三 domain、Domain SDK/plugin discovery、auth redesign、audit hash
  formation/ordinal 修改、新正式 approval/release 状态、第二条直接数据库工程写路径、
  用两个现有 write endpoint 顺序调用冒充原子性、deployment。

## 1. Q-persistence：change set 需要 durable persistence —— 需要，v16 单表加性迁移

**结论：需要 durable persistence；建议 v16，且仅在 D1 签署后由 D2 实现。**

理由：①approval binding 与 replay 防护要求 intent/approval 状态可复核、可失效追踪；
②post-commit evidence 必须 outlive 请求进程（M6/M12 先例：证据表无 drawing 生命周期 FK）；
③frozen two-domain acceptance 要能在任意时刻重放审计链。ephemeral（进程内/请求内）方案
无法满足②③，拒绝。

**v16 提案（D2 实现，单表加性、零 backfill 语义）**：

```
project_change_sets (
    change_set_id   TEXT PRIMARY KEY,        -- 'cs_' + uuid4 hex[:12]
    project_id      TEXT NOT NULL REFERENCES projects(project_id),
    status          TEXT NOT NULL CHECK(status IN
                    ('staged','approved','applied','refused','superseded')),
    base_pins       TEXT NOT NULL,           -- canonical JSON {document_id: revision}
    intent          TEXT NOT NULL,           -- canonical JSON：project_id+base_pins+mutations
    intent_hash     TEXT NOT NULL,
    impacted        TEXT NOT NULL DEFAULT '{}',  -- impact analysis 结果快照（canonical JSON）
    preview         TEXT NOT NULL DEFAULT '{}',  -- shadow preview 结果快照
    result_pins     TEXT NOT NULL DEFAULT '{}',  -- applied 后 {document_id: revision}
    evidence        TEXT NOT NULL DEFAULT '{}',  -- per-domain diff hashes / link snapshot /
                                                 -- readiness result_hash / audit refs
    session_id      TEXT, approval_id TEXT, tool_call_id TEXT,  -- M10 绑定（applied 后填）
    created_at      TEXT NOT NULL, created_by TEXT NOT NULL,
    updated_at      TEXT NOT NULL
)
```

- 迁移 `_migration_16`：FK 保持 ON、单事务、显式 rollback（照抄 v15 纪律）；无任何 backfill
  （新表空表即正确）；upgrade fixture（v15 库 → v16 → 断言表结构与 user_version）；
  备份链复用 R78-1 口径（v15 binary 备份 → v16 binary 迁移 → restore 得 v15 → 再迁）。
- 状态机（最小）：staged（impact/preview 完成）→ approved（exact approval 绑定）→ applied
  / refused；superseded 留作未来、M13 不产生。无 Approved/Released 工程语义。

## 2. Change Set Contract（Q-contract）

```python
ChangeSetIntent {
  project_id: str
  base_member_pins: {document_id: int}          # 声明的交付前状态（CAS 边界，同 D81-1）
  mutations: [                                   # 有序；顺序即应用顺序
    {
      domain: 'pid'|'cable'
      document_id: str
      kind: 'pid_transaction'|'cable_update'    # M13 先冻结这两种；link 变更经 impact 派生
      payload: <domain canonical op 列表 / cable document patch>
    }
  ]
}
canonical_intent = canonical_json(intent)        # sort_keys/无空白/ensure_ascii=False
intent_hash = sha256(canonical_intent)
```

- link 的 create/re-pin/delete **不直接出现在 mutations**：由 impact analyzer 按工程语义
  派生（见 §3），用户只声明工程意图（加设备、加电缆段），系统推导链接动作。这保证
  impacted identities 永远可解释。
- 结构上限：M13 冻结 mutations ∈ {pid_transaction, cable_update}，每域每文档至多一条。

## 3. Deterministic Cross-Domain Impact Analysis（Q-impact）

纯函数 `analyze(intent, project_graph) -> ImpactResult`，确定性排序，无时钟：

1. **输入解析与 fail-closed**：mutation 目标必须是 project member 且声明 pin == 当前
   revision（否则 stable `impact_base_not_current`）；目标对象必须存在（pid element /
   cable segment，否则 `impact_unknown_object`）；kind 白名单外 → `impact_unknown_kind`。
   未知/歧义一律拒绝，绝不猜测。
2. **文档级影响**：affected_documents = mutation 目标 ∪ 被影响 link 的对端文档。
3. **对象级影响**：对每个 mutated pid element——经 active engineering links 查
   cable 端（segment+endpoint）；对每个 mutated cable segment——查其两端 link 的 pid
   equipment。link 的 pinned object 若因本 change set 消失/改变类型 → 派生 link 动作
   （re-pin 到 result revision；对象消失 → 标记 stale 由 validator 报告，M13 不自动删）。
4. **validation scope**：affected set 上重算 project readiness 规则子集（M12 七 code 中
   与 affected 文档相关的部分）+ 每域单域 readiness（复用 D4 canonical 函数）。
5. 输出：`impacted = {affected_documents, affected_objects, affected_links, derived_link_actions,
   validation_scope, issues:[stable codes]}`——任何问题（含 fail-closed 拒绝）都进 issues，
   调用方不得忽略。

## 4. Multi-Domain Shadow Preview（Q-preview）

零写入承诺：preview 全程不触碰 store 写路径；测试断言 audit 计数零增长。

- pid 域：在内存 document 副本上走既有 stage/validate 管线（`_stage_mutation` 语义），
  产出 staged document + semantic diff（复用 semantic diff 纯函数）+ revision projection
  （base+1）。
- cable 域：CableDocument 不可变更新（`with_segment` 等纯函数），serialize 后得
  projection。
- link 影响：对 §3 派生动作做 dry 推演，输出 link impact（哪些 link pins 会 stale、
  哪些需 re-pin）。
- readiness delta：在 staged 快照上以纯函数重算受影响规则（§3.4），输出 before/after
  issue 集合与 project state delta；**preview 的 readiness 是投影而非裁决**，applied 后
  以 D4 assess 的真实值为准，preview 输出中明示此边界。
- 输出快照随 change set 持久化（§1.preview），e2e 比对 preview==applied 前投影。

## 5. Exact Intent Approval Binding（Q-approval）

完全复用 M10 与 D79-2 机制，零新语义：

- 中性 tool `apply_project_change_set`（permission=ask, risk=engineering_change），
  canonicalize 时由服务器注入**当前** base pins 与 impacted 摘要（调用方字段剥离重算，
  同 D79-2），`tool_intent_hash` 锁批准状态。
- apply 时重 canonicalize + hash 比对：任何 member revision、mutation、affected identity
  或 intent 变化 → `change_set_conflict` 稳定 409，零写入。
- approval 单次消费（既有 consumed 语义）+ base_pins == 当前 revision 双闸 → 天然防
  replay。

## 6. Atomic Governed Multi-Domain Commit（Q-atomicity）

**关键事实：P&ID、Cable、links、audit 同处一个 SQLite 库 → 真原子性 = 单事务。**

- `store.commit_project_change_set(...)`：BEGIN IMMEDIATE 单事务内顺序执行——每域 staged
  文档 CAS 写（expected_revision=base pin，失败即 rollback 全部）、派生 link 动作
  （re-pin/invariant 校验，D3 不变式在写连接上重验）、change set 行状态机推进
  （approved→applied，写 result_pins/evidence）、tool-call/approval/session closeout、
  governance audit（新事件类型 `project_change_set.applied`/`.refused`，治理平面）。
- 任何一步失败：整体 rollback + failure closeout（稳定码 + failed tool call + failed
  session + 恰好一条 rejected 审计）——D79-1 既有纪律平移，无半项目状态可能。
- **显式禁止**顺序调用 pid/cable 两个 write endpoint；HTTP 层只暴露 change-set 一个
  写入口（D5 的 read surface 之外）。

## 7. Post-Commit Project Evidence（Q-evidence）

evidence JSON 冻结字段：before_pins / after_pins（=result_pins）/ per-domain
{semantic_diff_hash, staged_payload_hash} / link_snapshot（applied 后 active links canonical）
/ readiness_result_hash（D4 assess 真实值）/ audit_record_ids（session/approval/tool_call/
governance）/ intent_hash。applied 后 result pins == 各文档当前 revision → **直接满足
M12 package 的 member pins 前置**，e2e 以同一 pins 构建 package 完成闭环。

## 8. 测试矩阵（请 D1 Design Gate 一并冻结）

1. backend：contract canonical/hash 确定性；impact 正负例（unknown object/kind/base stale）；
   preview 零 audit + 投影确定性；approval binding/replay/冲突；atomic commit 成功 +
   每域 CAS 失败/failure injection 全回滚（无半状态断言：对端文档 revision 不变）；
   evidence 字段完备；v16 迁移/回滚链（若 D1 批 v16）。
2. e2e（frozen，真实后端）：seed 双域成员+link → 建 change set（加 pid 设备 + 加 cable
   段）→ preview（零写入断言）→ exact approval → atomic apply → D4 readiness → 用
   result pins 构建 M12 package → HTTP==UI==fresh-process 字节；failure injection /
   revision race / membership drift / approval replay / partial-commit 负例。
3. shared-mode：匿名 change-set 写族 401，token 全链可用；read surface 不退化。
4. 非回归：全量零删测零放宽；M5 gate / P&ID / Cable / M12 package 不退化。
5. 性能：pre-M13 CI baseline ≤ +10%（基线 = main run 37210734487 的 pytest exact log，
   沿用已批 amendment 口径；D2 起在 reports/m13-perf-baseline.json 钉值）。

## 9. 分片与请求签署

代码顺序（D1 PASS 后）：D2 v16 迁移+change-set 持久化原语 → D3 impact analyzer + shadow
preview → D4 exact approval + atomic executor（独立 Merge Gate，风险最高）→ D5
evidence/read API/UI + frozen e2e + closeout。

请 Gate 逐项裁：§1（v16 与否与表结构）、§2 契约、§3 impact 算法、§4 preview 语义、
§5 approval 复用、§6 原子提交（单事务）、§7 evidence、§8 测试矩阵。签署后合并本 PR
（Charter 1.2.0 落地），持 D2 CODE GO。
