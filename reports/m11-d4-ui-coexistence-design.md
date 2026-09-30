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

## 5. E2E 双域共存（真实 local backend，禁 mock）

① seed 一个 P&ID 文档 + 一个含 segment 的 Cable；② 打开 P&ID workspace 选中并确认；③ 切「线缆」→ 列表只见 Cable 不见 P&ID；④ 点入详情见 segment + readiness；⑤ 点 export 完成真实 ZIP 下载；⑥ Cable 页无 add/edit/delete 控件；⑦ 切回 P&ID 原文档 revision/content 不变；⑧ Cable revision/content 不变；⑨ audit count 不因 Cable UI 读/下载增长；⑩ shared-mode 下 Cable GET 走现有 auth/request 边界（不成为匿名旁路，复用既有中间件，硬测 401/403 路径）。

## 6. D4 Closeout 矩阵（固定格式，直接回答 M11 Definition）

| M11 completion candidate | Evidence |
|---|---|
| Cable persistence | D1 |
| Cable validation/readiness | D3 |
| Harness/runtime governed write | D2 |
| Minimal Cable UI | D4 |
| Deterministic export | D3 + D4 HTTP byte parity |
| P&ID non-regression | D1–D4 CI + D4 共存 e2e |
| Same-deployment dual-domain coexistence | D2 backend isolation + D4 API/E2E |
| M11 closeout decision | D4 closeout matrix |

并固定声明：M11 implementation closeout ≠ M9 WS3B closeout；D4 完成不补齐真实项目试点；不改变 M10 FINAL ACCEPTANCE 的 Owner 阻断状态。

## 7. Whitelist / 禁项

Whitelist：api_cable.py（3 GET + 错误映射）、store cable 枚举只读方法、main.py 仅 router composition、surface_contract.py + contract 测试、Cable API 测试、frontend/src/cable/*、App.tsx tab wiring、api.ts 只读 client、frontend 测试 + Playwright 共存 e2e、D4 closeout 报告。
禁：schema v15 / runtime ports/models / audit chain 语义 / P&ID domain behavior / Cable 写 REST|MCP|UI / 新 approval 模型 / Router 迁移 / production deploy。

请裁 M11-D4 Design；PASS 请签 M11-D4 CODE GO。
