# M11-D3 详细设计 v2：Cable validator profile + deterministic export artifact（送 Gate 审，docs-only）

> 前置：M11-D2 CLOSED（main@03ec9c9）；D3 DESIGN PREP GO 已签。基线 main@03ec9c9e5be6a7ad41d8b2c8edba1c03d720e675。两个核心问题：Cable 如何**复用现有 validation 契约而不复用 P&ID 引擎**；两成员确定性 ZIP 如何**同 revision 真正 byte-for-byte 可复现**。

## 1. validation 与 export 解耦（冻结）

- validation/readiness = 「当前 revision 是否满足自动规则」，**自动 eligibility，不是人工审批**（沿用 ReleaseReadiness 的 eligible/not_eligible 表达，永远不得出现 Approved/IFC/AFC）。
- export = 「把当前 exact revision 确定性导出」。**readiness not_eligible 仍可导出**（供检查）；artifact 不得自称 released/approved。
- 两概念互不阻塞：validator 不 gate export；export 不修改任何状态。

## 2. Cable validator（复用契约、不复用引擎）

**不复用** validation_engine 的 P&ID 规则/目录；**复用**其契约形状（result/readiness 字段语义、hash 纪律）。

### 2.1 分层（R11-D3-1 冻结）：结构不变量 ≠ validator rules

- **结构不变量（parse 层 fail-closed）**：schema 为合法 literal、segment id/from/to 非空、from≠to、id 文档内唯一——这些由 CableDocument 模型在**加载前**强制；**解析失败不产生 readiness result**（raise，绝不输出看似正常的 assessment）。
- **validator profile v1 只含两条 post-load readiness 规则**（不为凑数复制结构真相）：

| rule_id | severity | 语义 |
|---|---|---|
| `cable.gauge_grammar` | blocker | gauge 满足冻结语法+语义（见 2.2） |
| `cable.document_non_empty` | warning | 文档至少一个 segment |

- **release policy 冻结**：eligibility = 无未豁免 blocker **且** `also_fail_on_warning=["cable.document_non_empty"]` 无 finding——空文档 warning 同样使 not_eligible（消除「warning 却 eligible」的漏洞）。

### 2.2 gauge 语法+语义（R11-D3-2 精确冻结，fullmatch）

- 语法（二选一，不存在 `2.5AWG12` 这类拼接）：
  `^(?:\d+(?:\.\d+)?mm2|AWG\d{1,2})$`（fullmatch，大小写敏感）。
- 语义（正则之外显式校验，「能匹配」≠「合法」）：
  mm2 分支数值必须 **> 0**（`0mm2`、`00mm2` 拒绝；小数位 ≤2 由语法保证）；
  AWG 分支数值必须在 **0–40**  inclusive（`AWG99` 拒绝）。
- 违反任一为 `cable.gauge_grammar` blocker finding。

### 2.3 profile / result 冻结

- `CableProfile(profile_id="cable-built-in", profile_version=1, rules=[...上表...])`；**unknown rule_id 或 profile_version 非 1 → fail-closed**（整个 assessment 返回 not_eligible + reason，绝不静默丢规则）。
- profile fingerprint = sha256(canonical JSON of {profile_id, profile_version, sorted rule ids + severity})，canonical 排序冻结。
- `CableValidationResult`：document_id、revision（envelope 权威值）、content_hash、profile_id/version/fingerprint、rule_results 每条 {rule_id, severity, passed, detail}、counts、**result_hash**。
- result_hash 输入 = {profile fingerprint, document_id, revision, content_hash, sorted rule_results}；**排除** timestamps/运行序号——同一 revision 结果 hash 必须可复现。
- content_hash = sha256(canonical JSON of CableDocument payload 不含 revision) + revision 绑定：`sha256(revision || ":" || payload_hash)`。
- readiness = unwaived blocker 存在 → not_eligible；仅 warning → eligible。waiver 机制 v1 不提供（有 waiver 语义前先 fail-closed）。
- validator **只读**：读 cable envelope，不改 Cable/P&ID 任何状态、不写 audit。

## 3. deterministic export artifact（两成员 ZIP，冻结）

### 3.1 成员与格式

| 成员 | 内容 |
|---|---|
| `cable-document.json` | canonical JSON（CableDocument payload 不含 revision + 本次服务端计算的 `revision`/`content_hash`/`validation` 段；validation 段含 result_hash 与规则明细） |
| `MANIFEST.sha256` | **恰 1 行**：`<sha256(cable-document.json)>  cable-document.json\n`（R11-D3-3——不 hash 自身，消除自引用环） |

完整 ZIP 的 `artifact_sha256` 在 ZIP bytes 完成后由服务器计算，作为函数返回 evidence，**不写回 ZIP**。

### 3.2 确定性纪律

- canonical JSON：ensure_ascii=False、sort_keys、紧凑分隔符、LF、末行换行；**全部 hash 服务端重算**，调用方提交的任何 hash 一律拒绝作为事实源（参数中出现即 422）。
- **ZIP 元数据全冻结（R11-D3-4）**：ZIP_STORED 不压缩；成员恰两枚、插入序 cable-document.json → MANIFEST.sha256、无目录项；ZipInfo date_time=(1980,1,1,0,0,0)、create_system=0、external_attr=0、permissions 不外泄、extra=b""；archive comment 空、member comment 空；ASCII 文件名。
- **byte-for-byte 复现（R11-D3-4 加强）**：同 document_id + expected_revision 在**两个独立 fresh Python 进程**中导出，完整 ZIP bytes 与 SHA-256 相同（同进程双跑只是必要条件，跨进程才是证据）。
- **敏感受输入影响**：payload 或 revision 任一变化 → artifact 内容与 bundle hash 必变（硬测：改一字符后 hash 不同）。
- 导出接受 `expected_revision` 参数做 **stale CAS**（envelope revision ≠ expected → typed error `stale_revision`）；**API 不提供任何 caller-hash 参数**——没有入口比「收进来再拒绝」更安全（R11-D3-5）。
- **内部 typed errors（无 HTTP 语义，R11-D3-5）**：`stale_revision` / `caller_hash_not_allowed`（防御性保留，正常不可达）/ `invalid_cable_payload` / `unsupported_profile_version` / `unknown_rule`。将来公开 REST surface 再映射 409/422；D3 模块层零 HTTP 概念。
- manifest/payload 篡改可检测：导出端点对已生成 artifact 的重校验例程（成员集合、逐成员 hash、manifest 行格式）供测试与将来服务端复用。

### 3.3 边界

- 导出只读；Cable id 送入 P&ID 导出/validator、P&ID id 送入 Cable 导出/validator → 各自 fail-closed（not_found，零状态变化）。
- D3 仍禁：schema v15 / cable_segments / node 表 / runtime ports/models / P&ID validator 规则行为 / public REST/MCP / UI / 人工 approval-release 状态。

## 4. 测试矩阵（冻结 ≥10 条）

① 五规则全过 → eligible；② gauge 违例 → not_eligible 且 reason 点名规则；③ 空文档 → eligible-with-warning（document_non_empty 仅 warning）；④ unknown rule id / profile_version → fail-closed not_eligible；⑤ result_hash 同 revision 可复现、跨 revision 必变；⑥ 双导出 byte-for-byte 一致；⑦ payload 改一字符 artifact hash 变；⑧ manifest/payload 篡改校验例程报警；⑨ expected_revision stale → 409；⑩ validator+export 全程 P&ID 文档/revision/digest/audit 零变化 + 跨 domain id 互送 fail-closed。

## 5. 待裁决策点

1. §2 规则集与 gauge 语法；2. §3 两成员契约与 stale CAS 口径；3. whitelist 预告：新增 cable_validation.py / cable_export.py + tests；不动 validation_engine/validation_profile 生产语义（只读复用其模型形状）、不动 runtime/schema/P&ID。

请裁 M11-D3 Design；PASS 请签 M11-D3 CODE GO。
