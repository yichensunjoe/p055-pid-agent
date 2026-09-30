# M11 Milestone 定义提案 v2（Charter MINOR revision §55A，送 Gate 审定义）

> Gate：M11 DEFINITION PREP GO = APPROVED（2026-10-01）；v1 被裁 CHANGES REQUIRED（R11-DEF-1~4），本版逐项闭合。本文档纯定义/设计提案，零产品代码；按 Charter §26.3（Owner 主动要求 = replanning trigger）与 §57/§59（新增 milestone = MINOR revision）提交。基线 main@3379477323e512dd29ec3e28b7e6c312761e52b6。

## 1. 名称与目标

**M11 — Production-Ready Second Domain Slice（§55A）**：把 M10 已 ACK 的平台解耦从「测试证明」推进到「生产可用切片」——**Cable Schematic v1（冻结，不换 domain，换则重走 definition revision）** 以持久化、可校验、可导出的产品形态跑在 runtime 上，P&ID 能力全程不退化。新增 §55A，不改 §55 原文、不改 M10 Technical Completion ACK（R11-DEF-1）。

## 2. 为什么需要 M11，而不是 M10 的遗漏工作

M10 的完成标志（Charter §55）被有意切成两层并已 ACK：技术实现证据完整（P1–P4）。但 §55 同句还有「第二个 engineering drawing domain 能复用 runtime」——P3/P4 证明的是**测试态**复用（内存 fake + test-only cable），不是**生产态**复用。把「生产态第二 domain」塞进 M10 会违反 M10 各 PR 的 Gate 冻结范围（P1–P4 全程禁产品化第二 domain）；它是独立的工程量（schema 迁移、持久化、validator profile、UI/导出面、双 domain 共存运维），不是 M10 遗漏。M9 的剩余（WS3B 试点）是**人的使用验证**，M11 是**平台的产品化验证**——两者互补且不重叠。

## 3. 当前真实瓶颈

- runtime 核心已领域无关且经隔离锁证明，但 persistence/audit 的实现类（SQLiteDocumentStore/AuditRecorder）仍与 P&ID 文档模型同库同表耦合——第二 domain 生产化需要治理面（session/approval/tool-call/audit）与文档面的存储边界真正分开。
- Cable 证明切片没有持久化、没有 validator、没有 UI/导出——不足以回答「第二 domain 能复用 runtime」的生产含义。
- 无跨 domain 的非回归基准：改动 runtime 时如何证明 P&ID 不退化已有机器锁（1706 测试），但**新增 domain 数据面**对 P&ID 的干扰尚无基准。
- **domain-neutral document identity boundary 缺失（R11-DEF-2）**：现有三张治理表（agent_sessions/agent_approvals/agent_tool_calls）的 document_id 均 FK 指向 P&ID documents 表——非 P&ID 文档无法合法进入 session/approval/tool-call 链。D1 的核心设计问题就是这条边界（generic document registry / 治理 FK 重构等方案开放，不在定义期锁实现）。

## 4. 完成标志（冻结候选，待 Gate 裁）

1. **Cable Schematic v1**（domain 已冻结，换 domain 须重走 definition revision）达到 production-ready minimal slice：持久化（**同一 SQLite 库内 domain 独立表**，R11-DEF-3；跨独立库属未来扩展，不进 M11）、自身 validator profile（release-readiness 可跑）、最小 UI 面（画布/列表面）、**一个 deterministic export artifact（格式在 D3 Design Gate 预先冻结，不事后挑选）**。
2. 该 domain 的 governed write 全程走 M10 runtime（session→authorize→approval→execute→audit），审计链接入**同一条全局 hash chain**（可交叉验证两 domain 的审计完整性）。
3. P&ID 非回归：1706 测试全绿 + M5 deterministic + e2e + surface contract 机器锁，外加新增的「双 domain 共存」硬测（P&ID 文档操作与第二 domain 操作交错，互不改写对方状态）。
4. 文档/迁移：按 D1 Design Gate 冻结的 v14 migration-safe 方案完成 v13→v14 upgrade、backup/restore 演练与 compatibility evidence；Charter 新增 §55A，不修改 §55。

## 5. 与 M9/M10 的依赖关系

- 依赖 M10 全部（已 ACK）。
- **不依赖 WS3B/M9 Closeout**；与之完全并行。M11 的完成不需要真实工程项目试点（那是 M9 的完成标志，口径已冻结）。

## 6. 明确声明：不改变 WS3B/M9 Closeout

本提案不动 M9 任何状态（WS1/WS2 CLOSED、WS3B BLOCKED ON OWNER、Closeout PENDING 保持），不动成熟度门甲，不动 M10 FINAL ACCEPTANCE 的 Owner 依赖。M11 是并行新里程碑。

## 7. Immutable Core / P0 影响

- 不动：工程交付确定性、semantic-first、tool-mediated editing、deterministic validation、audit/rollback（全局 hash chain 语义不变）、model-agnostic、人工正式审批边界（operator 治理面语义原样迁移）。
- P0 风险点：审计链跨 domain 的 ordinal 全局序必须不变；若 M11 需要动 audit hash formation/chain schema——**先停，回 Gate**，本提案默认不动。

## 8. Mutable Architecture 范围

- store 层抽出「治理面存储端口」的实现边界（M10 已有 HarnessStorePort；M11 新增文档面按 domain 分表的存储规划，migration-safe v14）。
- 第二 domain 的 adapter/validator/UI/导出为**新增模块**，不修改 P&ID 现有模块行为。
- main.py composition root 扩展（双 domain 挂载）。

## 9. Benchmark before（已在 M10 收口测得）

main@3379477：backend 1706 passed、ruff 净、e2e 5/5、main CI 36655549044 四绿；全量墙钟 ~105s（本地，`pytest backend/tests -q -p no:cacheprovider` + 同一 venv/机器；after 对比必须用相同命令与环境，R11-DEF 收紧项）。

## 10. Expected benchmark after

- pre-M11 baseline 测试不得删除/xfail/skip；新增 M11 测试 ≥20 条（持久化/迁移/双域共存/导出/validator）。
- 全量墙钟增量 ≤ +10%（~115s 内）。
- 双 domain 共存硬测：交错操作 100 轮零交叉污染（属性测试）。
- CI 仍为四 job 全绿。

## 11. Migration（R11-DEF-2/4 修正后口径）

- v14 必须 data-compatible / migration-safe；**是否 DDL 纯 additive 由 D1 Design Gate 决定，定义期不预设**。
- D1 必须先设计 domain-neutral document identity boundary，再谈 Cable 表结构。
- 同一 SQLite 库、domain 独立表（R11-DEF-3）；audit hash formation / chain schema / global ordinal 完全不动。
- 迁移必须原子、失败自动回滚；v13→v14 需 upgrade fixture + 迁移锁测试；**pre-v14 backup → restore 必须演练**。

## 12. Rollback（R11-DEF-4 修正后口径）

- D2/D3/D4 为独立 code revert，逐片可回退。
- **D1 的 schema migration 是 forward migration：不承诺「git revert = DB rollback」**——v14 库不能被 v13 binary 打开（DatabaseVersionError）；若已产生 Cable v14 数据，降回 v13 = **恢复 migration 前 backup / 专门 downgrade 演练过的 procedure**，不是「表弃用即可」。
- v14 上线前必须有可验证 backup；回滚剧本随 D1 Design 一起交 Gate。

## 13. Data / schema compatibility

v13 数据 100% 兼容；audit chain 从 genesis 验证不变；第二 domain 数据不进入 P&ID 任何 digest/投影。

## 14. 工程 / 发布风险

| 风险 | 缓解 |
|---|---|
| 第二 domain 范围蠕变成新产品 | 完成标志只要求 minimal production slice；每 PR 对照 §4 逐条 |
| 存储边界改动波及 P&ID | D1 独立 PR + 全量回归 + 迁移锁 |
| 跨 domain 审计序混乱 | ordinal 全局序硬测；hash chain 验证跨 domain |
| Owner 试点四字段与 M11 抢优先级 | 完全并行，无资源冲突（M11 不消耗 WS3B 任何东西） |

## 15. 哪些是实现、哪些是证据

实现：存储边界、第二 domain 四件（持久化/validator/导出/UI）、composition root、共存基准。证据：新增测试、benchmark 前后对比、迁移锁、双 domain 审计链交叉验证。

## 16. Owner approval point

三处：① 本定义提案（当前，Gate 审）；② D1 合并前的 schema v14 迁移方案（Gate 审）；③ M11 完成标志逐条核对时的 final Gate。M11 不依赖 WS3B 四字段或真实试点输入；Charter revision、schema migration 和 M11 final acceptance 仍按既有 Gate/Owner approval governance 执行。

---

请裁：M11 定义（名称/完成标志/依赖/风险）APPROVED / CHANGES REQUIRED；若 APPROVED，请签 M11-D1 DESIGN PREP GO 或直接给 D1 CODE GO 范围意见。
