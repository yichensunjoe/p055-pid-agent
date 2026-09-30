# M11-D4 详细设计：Cable Read-Only UI Surface & Dual-Domain Coexistence Closeout（送 Gate 审，docs-only）

> 前置：M11-D3 CLOSED（main@01cf1ee）；D4 DESIGN PREP GO 已签（含 scope amendments）。基线 main@01cf1ee2fea3dc44a1af1cae2297d0f1dd5bf9bb。这是 M11 最后一片。

## 1. 写 UI 裁决（冻结）：不进 D4

Cable UI **无**新增/删除/修改 segment 控件；禁止为 UI 增加直接 Cable mutation endpoint。写 UI 将来若做，必须接 D2 已冻结的 governed runtime 路径并单独过 Gate。

## 2. 最小只读 Cable HTTP surface（冻结 3 GET）

```
GET /api/v2/cable/documents                          → [{document_id, name, revision, readiness_state}]
GET /api/v2/cable/documents/{document_id}            → {document_id, revision, schema, name, segments,
                                                        readiness: {state, counts, reasons, result_hash, profile_*}}
GET /api/v2/cable/documents/{document_id}/export.zip?expected_revision=N
                                                     → D3 ZIP bytes 原样（HTTP 层不重打包、不改一字节）
```

- detail 直接内嵌 D3 readiness（state/counts/reasons/result_hash/profile 三元组），不为 badge 再开 readiness 端点。
- 全部 `read` 类别：no side effect / no runtime approval / no audit event；进入 surface_contract 机器锁（`_read` 绑定 + 测试锁 3 路由存在且非 mutating）。
- store 最小新增：cable 文档枚举（纯读 SELECT document_id/revision + payload 摘要）。

**HTTP 错误映射（设计期锁死）**：cable 不存在 / P&ID id 误入 → 404；stale expected_revision → 409；query 类型非法 → FastAPI 框架 422；**库中 Cable payload 损坏 → 500 data-integrity**（D3 内部 invalid_cable_payload 不得映射 422——那是服务器数据坏了，不是用户提交非法 JSON）；SQLite/存储意外故障 → 500 保持真实故障。

## 3. UI：现有壳内 domain tab（冻结，不迁 Router）

`[P&ID] [线缆]` 顶层双 tab。进「线缆」：P&ID 画布不渲染；主区 Cable 文档列表 → 详情（segments 表 + readiness 徽章 + export 按钮）；返回 P&ID：原选中 P&ID 文档 identity 保留，切换不创建/修改/重导入任何 P&ID 文档。Readiness 徽章**只显示 eligible / not_eligible**，禁止 Approved/Released/IFC/AFC 字样（D3 readiness 只是自动 eligibility）。前端新增 `src/cable/`（列表/详情/徽章/导出下载）+ api.ts 只读 client + App.tsx 最小 tab wiring；不动路由架构、不动 P&ID 组件。

## 4. Export byte parity（硬锁）

HTTP export 返回的字节与 `export_cable_document()` 同 revision 产物**逐字节一致**（测试断言：HTTP body == 直接调 D3 函数的 zip_bytes）；全局 audit chain verify 继续成功。

## 5. E2E 双域共存（冻结 10 步，真实 local backend，禁 mock）

① P&ID baseline：seed 一个 P&ID 文档并在 workspace 选中确认（记录 revision/content）；
② 切「线缆」tab；
③ 真实 Cable list（DB 驱动）只见 Cable、不见 P&ID；
④ 点入 detail：load/identity 正确（document_id/revision/schema/name）；
⑤ readiness 投影与 D3 assess() 逐项一致（state/counts/result_hash/profile 三元组）；
⑥ export byte parity：HTTP body 与直接调用 D3 export_cable_document() 同 revision zip_bytes 逐字节一致；
⑦ stale expected_revision → 409；
⑧ P&ID document id 打 Cable endpoint → 404；
⑨ 切回 P&ID：原文档 intact 且仍可编辑，Cable revision/content 亦不变；
⑩ shared-mode：Cable GET 走现有 auth/request 边界（非匿名旁路）。
附加断言（步骤内）：Cable 页无 add/edit/delete 控件；任何 Cable UI 读/下载不增 audit、不改任何 revision/content。

## 6. D4 Closeout 矩阵（冻结 8 问，直接回答 M11 Definition）

| # | Definition 问题 | Evidence |
|---|---|---|
| 1 | true second domain | Cable Schematic v1 独立模型/载荷/存储面（D1 identity + D2 service） |
| 2 | shared infrastructure/runtime reuse | M10 runtime 七端口 + 原子 governed write 承载 Cable（D2） |
| 3 | legacy P&ID preserved | D1–D4 全量 baseline 非回归 + P&ID 模块零行为改动 |
| 4 | domain isolation | D2 双向 loader fail-closed + D4 e2e 步骤⑦⑧⑩ |
| 5 | production-real other-system slice | Cable production persistence + governed write + read-only surface（D1/D2/D4） |
| 6 | engineering semantics | Cable 领域 invariant（from≠to、id 唯一、gauge 语法语义）（D2/D3） |
| 7 | validation/readiness | D3 两规则 profile + state/counts 契约 |
| 8 | export delivery | D3 确定性 artifact + D4 HTTP byte parity |

并固定三项声明（冻结）：① expansion_threshold_reached——平台化扩展的阈值已由 D1–D4 证明达到（第二 domain 全链落地）；② workload mix 明文——M11 工作负载 = identity/migration（D1）+ runtime wiring/原子写（D2）+ validation/export（D3）+ 只读 surface/UI/共存证据（D4），全部围绕「第二 domain 复用 runtime」单一目标，无范围漂移；③ Cable Schematic 是真正第二 engineering drawing domain（独立图纸类型、独立载荷契约、独立 validator），不是 logical equipment 或 P&ID 内部对象伪装。
附加说明：M11 implementation closeout ≠ M9 WS3B closeout；D4 完成不补齐真实项目试点；不改变 M10 FINAL ACCEPTANCE 的 Owner 阻断状态。

## 7. Whitelist / 禁项（按冻结清单逐字复述 + 明确 amendment）

Whitelist（D4 PREP 冻结清单逐字）：backend/agentcad/api_cable.py；Cable service/store minimal read enumeration additions；backend/agentcad/main.py 仅 router composition；surface_contract.py + contract tests；Cable API tests；frontend/src/cable*；App.tsx 最小 domain-tab wiring；frontend/src/api.ts Cable read client；Cable frontend tests / Playwright coexistence E2E；D4 closeout report。
Amendment 声明（如启用冻结清单外路径需逐条列此处，当前无）。
禁项（两批冻结清单并集）：schema v15；runtime ports/models；audit-chain semantics；P&ID domain behavior changes；Cable write REST/MCP；Cable write UI；new Cable approval model；React Router 大迁移；production deploy；新建 file store；identity/validator/export 契约重定义；auth redesign；arbitrary refactor。

## 8. 治理口径（纠正）

M11 定义 = **Gate 批准的 definition proposal（自称 §55A 草案）**；PROJECT_CHARTER.md 当前正文仍是 §55→§56，**尚无 §55A 文字**。Charter MINOR revision（把 §55A 落入 Charter 正文）是单独的治理动作，已在 Gate 获批定义层面成立、但未提交 Charter 文本修订——D1–D4 代码事实不受影响（main=01cf1ee）。本设计稿所有「§55A」表述均指 Gate-frozen definition，非 Charter 现存条文。

请裁 M11-D4 Design；PASS 请签 M11-D4 CODE GO。
