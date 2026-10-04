# M13-D1 — Governed Multi-Domain Change Sets: Contract / Impact / Preview / Atomic Commit Design

> 状态：DESIGN FIX（R82 修订版，docs-only）。回答 Gate 冻结的问题；签署前均为 proposal。
> base = main@b9e125f（M12 implementation closeout COMPLETE）。
> 依据 PROJECT_CHARTER v1.2.0 §55C（本 PR 同批提交）与 §57 Revision Proposal Record。

## 0. 依赖与硬约束（复述冻结口径）

- 依赖 M12 closeout；继承 M10 runtime/permission/approval/audit；复用 M11 双真域、M12
  project graph / readiness / deterministic package。不依赖 WS3B/M9 Closeout/M10 FINAL；
  不重开 M10/M11/M12 sealed 状态。
- 禁项（D1 及全程）：第三 domain、Domain SDK/plugin discovery、auth redesign、audit hash
  formation/ordinal 修改、新正式 approval/release 状态、第二条直接数据库工程写路径、
  用两个现有 write endpoint 顺序调用冒充原子性、deployment。
- **M13 v1 收窄**：不自动 create/delete engineering link（那是猜测关联，M12 禁令）；
  只允许自动派生 **existing active link 的 re-pin**；无法安全 re-pin 的链接对象失效
  → change set fail closed。未来 link create/delete 走显式 `engineering_link_change`
  intent（携带完整 source/endpoint/target identity），单独扩展。

## 1. Q-persistence：change set 需要 durable persistence —— 需要，v16 单表加性迁移

**结论：需要 durable persistence；建议 v16，且仅在 D1 签署后由 D2 实现。**

理由：①approval binding 与 replay 防护要求 intent/approval 状态可复核、可失效追踪；
②post-commit evidence 必须 outlive 请求进程（M6/M12 先例：证据表无 drawing 生命周期 FK）；
③frozen two-domain acceptance 要能在任意时刻重放审计链。ephemeral 方案拒绝。

**v16 提案（D2 实现，单表加性、零 backfill 语义）**：

```
project_change_sets (
    change_set_id   TEXT PRIMARY KEY,        -- 'cs_' + uuid4 hex[:12]
    project_id      TEXT NOT NULL REFERENCES projects(project_id),
    status          TEXT NOT NULL CHECK(status IN
                    ('staged','approved','applied','refused')),
    base_pins       TEXT NOT NULL,           -- DECLARED pins（canonical JSON，见 §2 铁律）
    intent          TEXT NOT NULL,           -- canonical JSON：project_id+base_pins+mutations
    intent_hash     TEXT NOT NULL,
    impacted        TEXT NOT NULL DEFAULT '{}',  -- impact 快照（canonical JSON）
    preview         TEXT NOT NULL DEFAULT '{}',  -- shadow preview 快照
    result_pins     TEXT NOT NULL DEFAULT '{}',  -- applied 后 {document_id: revision}
    evidence        TEXT NOT NULL DEFAULT '{}',  -- §7 冻结字段
    session_id      TEXT, approval_id TEXT, tool_call_id TEXT,  -- M10 绑定（applied 后填）
    created_at      TEXT NOT NULL, created_by TEXT NOT NULL,
    updated_at      TEXT NOT NULL
)
```

- 迁移 `_migration_16`：FK 保持 ON、单事务、显式 rollback（照抄 v15 纪律）；无 backfill；
  upgrade fixture；备份链复用 R78-1 口径（v15 binary 备份 → v16 binary 迁移 → restore
  得 v15 → 再迁）。
- 状态机（最小）：staged → approved → applied / refused。**`approved` 行本身不构成授权**
  （§5 铁律）：授权永远 = M10 approval 记录 + intent_hash 匹配 + 未消费 + base pins
  仍 == 当前 revision，四者同时成立。

## 2. Change Set Contract（Q-contract）

```python
ChangeSetIntent {
  project_id: str
  base_member_pins: {document_id: int}     # 唯一 CAS 来源（铁律 C1/C2）
  mutations: [                              # 有序；顺序即应用顺序
    {
      domain: 'pid'|'cable'
      document_id: str
      kind: 'pid_transaction'|'cable_update'
      payload: <domain canonical ops / cable document patch>
      # payload 内禁止携带任何 revision/expected_revision 字段（铁律 C2）
    }
  ]
}
canonical_intent = canonical_json(intent)   # sort_keys/无空白/ensure_ascii=False
intent_hash = sha256(canonical_intent)
```

铁律：

- **C1（declared pins 不得被静默替换）**：服务器独立重读当前 pins，要求 declared pins
  集合精确等于 project members 且每个 value == 当前 revision；不等立即拒绝
  （`change_set_base_not_current`）。验证通过后 canonical material 使用同一 declared
  pins——绝不把用户基于 revision A 的请求静默升级成 revision B 再让人批准。
- **C2（单一 CAS 来源）**：base_member_pins 是整个 change set 唯一的 revision 真源；
  mutation payload 不得携带 revision；若任何下层结构机械要求 revision 字段，必须断言
  它等于对应 base pin，冲突即失败。
- **C3（link 不声明不猜测）**：link create/re-pin/delete 不出现在 mutations；M13 v1 仅
  自动派生 existing active link 的 re-pin（§3）。

## 3. Deterministic Cross-Domain Impact Analysis（Q-impact）

纯函数 `analyze(intent, project_graph) -> ImpactResult`，确定性排序，无时钟：

1. **输入解析与 fail-closed**：mutation 目标必须是 project member（C1 已锁 pins）；目标
  对象必须存在（`impact_unknown_object`）；kind 白名单外（`impact_unknown_kind`）。
2. **文档级影响**：affected_documents = mutation 目标 ∪ 受影响 existing active links 的
   对端文档。
3. **对象级影响（仅 existing links）**：mutated pid element → 经 active links 查 cable
   端；mutated cable segment → 查两端 link 的 pid equipment。
4. **派生动作（唯一：re-pin）**：链接引用的对象在本 change set 后仍然存在且仍满足
   D3 invariants → 派生 re-pin 到 result revision（与工程写同事务）。
   **linked-object 失效 fail closed**：若 change set 会删除/改变被 existing active link
   引用的工程对象，以致 D3 invariants 无法安全 re-pin（对象消失、equipment predicate
   被破坏、endpoint/segment 不再合法）→ 整个 change set 拒绝
   （`change_set_invalidates_link`），不得「先提交坏引用、之后靠 validator 报错」。
5. **validation scope**：affected set 上重算 readiness 规则子集 + 每域单域 readiness
   （复用 D4 canonical 函数；§7 的纯函数内核）。
6. 输出：`impacted = {affected_documents, affected_objects, affected_links,
   derived_repin_actions, validation_scope, issues:[stable codes]}`。

## 4. Multi-Domain Shadow Preview（Q-preview）

零写入承诺（强化）：preview 前后**工程状态零变化**——不仅 audit 计数相等，所有 member
revision、link pins、文档内容哈希前后相等（e2e hard-lock）。

- pid 域：内存 document 副本走既有 stage/validate 管线；cable 域：不可变纯函数更新。
- link 影响：仅对 §3 派生的 re-pin 做 dry 推演；失效场景在 preview 阶段即 fail closed。
- readiness delta：对 staged 快照以 §7 纯函数内核重算，输出 before/after issues 与
  state delta；**preview readiness 是投影非裁决**，applied 后以 D4 assess 真实值为准。
- **evaluation_as_of 显式 parity**：preview 与后续 evidence 使用同一显式
  evaluation_as_of 输入，调用方不得省略由服务器默默取「现在」。
- 输出快照随 change set 持久化（§1.preview）；e2e 比对 preview 投影 == applied 后真实值。

## 5. Exact Intent Approval Binding（Q-approval）

复用 M10 与 D79-2 机制，另加铁律：

- 中性 tool `apply_project_change_set`（permission=ask, risk=engineering_change）；
  canonicalize 由服务器注入当前 pins + impacted 摘要（调用方字段剥离重算，同 D79-2），
  `tool_intent_hash` 锁批准状态。
- apply 时重 canonicalize + hash 比对：任何 member revision、mutation、affected identity
  或 intent 变化 → `change_set_conflict` 稳定 409，零写入。
- approval 单次消费 + C1（base pins == 当前）双闸防 replay。
- **C4（状态≠授权）**：change_set 行的 `approved` 状态只是持久化事实，任何时候都不
  能独立充当授权；执行前必须重新验证 M10 四要素（approval 记录存在、intent_hash 匹配、
  未消费、base pins == 当前）。

## 6. Atomic Governed Multi-Domain Commit（Q-atomicity）

**关键事实：全状态同一 SQLite 库 → 真原子性 = 成功单事务 + 独立的失败 closeout 事务**
（照抄 D79-1 两事务纪律，写清边界）：

- **成功事务（exactly one applied governance audit）**：BEGIN IMMEDIATE 内——每域
  staged 文档 CAS 写（expected_revision = 对应 declared base pin）；派生 re-pin（D3
  不变式写连接重验）；§7 evidence（含事务内快照 readiness hash）；change set 行
  approved→applied（result_pins/evidence）；completed tool call + approval consumed +
  completed session；恰好一条 `project_change_set.applied` 审计。
- **失败路径**：工程事务完整 rollback（任何 CAS/validator/link invariant/写入失败）；
  **随后另开 failure-closeout 事务**：change set → refused + failed tool call + failed
  session + 恰好一条 `project_change_set.rejected` 审计；不得残留任何 domain/link
  revision 变化。
- **显式禁止**顺序调用 pid/cable 两个 write endpoint；HTTP 只暴露 change-set 一个写
  入口。

## 7. Post-Commit Project Evidence（Q-evidence —— 时序冻结）

**矛盾**：§6 要求 evidence 与工程修改同事务；§7 要 evidence 含 D4 readiness 真实值，而
D4 assess 走新读连接、在 commit 前看不到未提交状态；commit 后再补 evidence 又产生
crash 窗口。

**冻结方案（纯函数内核）**：把 ProjectReadinessService 的规则内核抽成纯函数
`assess_project_readiness(members_state, links_state, evaluation_as_of, effective_profile)`
——D4 service 变为薄读取层（读库 → 喂内核）；commit 成功事务把 **staged 状态**喂同一
内核得到 `readiness_result_hash` 写入 evidence。同一内核 + 同一输入（staged == commit
后的 committed）⇒ 快照 hash 必然等于 post-commit D4 canonical hash（e2e hard-lock）。
evidence 冻结字段：before_pins / after_pins(=result_pins) / per-domain
{semantic_diff_hash, staged_payload_hash} / link_snapshot / **readiness_result_hash（事务
内快照）** / audit_record_ids / intent_hash / evaluation_as_of。applied 后 result pins
== 各文档当前 revision → 直接满足 M12 package 的 member pins 前置。

## 8. 测试矩阵（冻结 hard-locks，请 Design Gate 一并签署）

backend：
1. caller declared base pins 被静默替换必须失败（C1：集合/值任一不符即拒）；
2. nested revision 与 base pins 冲突失败（C2：payload 携 revision 即拒）；
3. explicit evaluation_as_of parity（省略/不一致即拒）；
4. impact 正负例（unknown object/kind/linked-object invalidation fail closed）；
5. preview 工程状态零变化（revision/pins/内容哈希前后相等）+ 投影确定性；
6. approval binding/replay/冲突（C4：approved 行单独不构成授权）；
7. 成功事务 exactly-one-applied-audit / 失败两事务边界（工程 rollback 后独立
   closeout，恰好一条 rejected 审计，无 domain/link 残留）；
8. 快照 readiness hash == post-commit D4 canonical hash；
9. v16 迁移/回滚链（D2，若批）。

e2e（frozen，真实后端，**场景修正**）：seed 已存在且被 active link 关联的 P&ID
equipment + Cable segment → change set 同时修改两端（如设备位号属性 + 段 gauge）→
preview（零写入断言）→ exact approval → atomic apply（**同事务 re-pin existing
link** 到 result revisions）→ D4 readiness → 用 result pins 构建 M12 package →
HTTP==UI==fresh-process 字节；failure injection / revision race / membership drift /
approval replay / partial-commit 全负例。

shared-mode：匿名 change-set 写族 401，token 全链可用；read surface 不退化。
非回归：全量零删测零放宽；M5 gate / P&ID / Cable / M12 package 不退化。
性能：pre-M13 CI baseline ≤ +10%（基线 = main run 37210734487 pytest exact log，沿用
已批 amendment；D2 起钉 reports/m13-perf-baseline.json）。

## 9. 分片与请求签署

代码顺序（D1 PASS 后）：D2 v16 迁移 + change-set 持久化原语 → D3 impact analyzer +
shadow preview → D4 exact approval + atomic executor（独立 Merge Gate，风险最高）→
D5 evidence/read API/UI + frozen e2e + closeout。

请 Gate 逐项复核修订版 §1–§8（§6/§8 条件通过项的措辞也已按裁定收紧）；签署后合并
#82（Charter 1.2.0 落地），持 D2 CODE GO。
