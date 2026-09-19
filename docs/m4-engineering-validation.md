# M4 工程校验系统（Engineering Validation System）规范

本文件是 **M4 的规范文本**，内容来自远端 Reviewer / Release Gate 2026-09-19 的基线
`REMOTE_BASELINE_2026-09-19_M4_GATE_R2.md` §5–§13 与 §9.3。Charter 完成条件见 `PROJECT_CHARTER.md` §49。

实现落点：

| 关注点 | 文件 |
|---|---|
| canonical 契约（issue/result/skip/waiver/readiness） | `backend/agentcad/validation_models.py` |
| 规则目录（稳定 `rule_id`、默认 severity、可配置阈值） | `backend/agentcad/validation_rules.py` |
| profile 分层、优先级、指纹 | `backend/agentcad/validation_profile.py` |
| validator registry、三个 adapter、确定性 run | `backend/agentcad/validation_engine.py` |
| release 就绪证据 | `backend/agentcad/release_validator.py` |
| 契约与边界测试 | `backend/tests/test_validation_contract.py` |

---

## 1. 目的与非目标

工程校验是**只读证据系统**，不是审批系统：

- 校验**不变更图纸**：不写 revision、不写 history、不写样式/几何；REST/MCP/CLI/UI 上均注册为 **read**。
- 校验**不产生正式放行状态**：`Approved` / `IFC` / `AFC` / 签名 / 人工批准只能由 Approval Gate 之后的人类流程产生（Charter §11、§44-10）。
- 校验**不自动修复**：`suggested_repair` 是建议证据，不是修改许可；自修复属于 M5。
- 校验**不做 CAD 语义推断**、不削弱规则让测试变绿、不用视觉相似度或模型置信度当工程真相（R2 §13）。

## 2. canonical schema

`ValidationIssue` 的公共字段（全部必存在，不允许“按需才有”）：

```text
code, severity(info|warning|error|blocker),
object_ids[], element_ids[],
message, expected, actual, suggested_repair,
rule_source, registered, threshold,
waiver_status(not_waived|waived|expired), waiver?,
validator_id, rule_id, profile_id, profile_version, details{}
```

`ValidationResult`：

```text
schema, version, document_id, revision, content_hash, project_id,
profile_id, profile_version, rule_bundle_fingerprint,
engine_version, symbol_registry_fingerprint, evaluated_at,
validator_versions{}, validators_run[], validators_skipped[],
issues[], counts{blocker,error,warning,info,total,waived,unregistered}, result_hash
```

`ValidatorSkip` = `validator_id` + 稳定机器码 `code`（如 `context_unavailable`、
`engineering_report_context_unavailable`）+ 人类可读 `reason`。`"未运行" 永远不等于 "通过"`。

`WaiverEvidence` = `waiver_id, actor, reason, granted_at, expires_at?, max_revision?,`
`object_ids[], element_ids[], status, selected_from`。证据里带**作用域**，因为审阅者需要看到
“这份批准覆盖的是这些元素”。

`ReleaseReadiness` = `state(eligible|not_eligible)` + 文档/配置/校验哈希 + `engine_version`、
`release_validator_version`、`symbol_registry_fingerprint`、`evaluated_at`、`counts`、
`unwaived_blockers[]`、`unwaived_failures[]`、`missing_required_validators[]`、
`unregistered_codes[]`、`waivers_considered[]`、`policy`、`human_approval_required=True`、
`readiness_hash`。

## 3. 稳定身份

- `object_ids` 是工程身份（M2 的 equipment/line/valve/instrument…）。
- `element_ids` 是图面上的定位 id。
- 两者**不是二选一**：同时知道的 issue 必须同时带上，缺一不可推断。

## 4. 确定性与 provenance

一次结果绑定：文档 id、revision、内容 hash、profile id/version、**完整 rule-bundle fingerprint**、
engine 版本、符号目录 fingerprint、以及显式的 **评估时刻 `evaluated_at`**。

- 确定性定义：`同一文档内容 + 同一 revision + 同一 profile/规则包 + 同一 validator 输入与版本 +
  同一评估时刻 ⇒ 同一 canonical 结果与同一 result_hash`。
- 时间是有输入的：waiver 会过期，所以“什么时候判的”必须写进结果，不能被墙钟偷偷决定。
- canonical 顺序：`severity rank(blocker<error<warning<info)` → `code` → `object_ids` →
  `element_ids` → `message` → `validator_id`；列表输出稳定可 diff。
- `result_hash` / `readiness_hash` 覆盖 canonical JSON（不含自身哈希），供审计与复现引用。

## 5. 规则目录

- `rule_id = <validator_id>.<CODE>`，例：`diagram-quality.NODE_OVERLAP`；`<validator_id>.*` 表示整个 validator。
- profile 中的**未知精确规则** ⇒ `ProfileError(code="profile_unknown_rule")`；
  **未知 validator（含 `release_policy.required_validators`）** ⇒ `profile_unknown_validator`。
  没有“未知规则先用着”的后门：拼写错误必须响亮地失败。
- adapter 发出**目录里没有的 code** 时：finding **不丢弃**，标记 `registered=False`、
  `rule_source="unregistered"`，计入 `counts.unregistered`，并让 release readiness **fail closed**；
  绝不伪装成 `built-in`。
- `ValidationIssue.threshold` 对阈值规则填入**生效阈值**，非阈值规则为 `None`。

## 6. profile 与优先级

规范链（弱 → 强）：

```text
built-in < standard < company < project < release-phase
```

- profile 是**服务端**文件（`PID_AGENT_VALIDATION_PROFILE`），不接受请求体传入；Agent 不能给自己关规则或发 waiver。
- 只有 typed `RuleOverride`（`enabled` / `severity` / `threshold`）；无 `eval`、无脚本、无 prompt 规则。
- 非法配置一律拒绝：层级重复、层级乱序、项目未授权却声明 release-phase 覆盖、阈值不可配置、
  阈值非有限数（NaN/Infinity）、质量分阈值超出 0..100、重复 `waiver_id`。
- `allow_release_phase_overrides` 是 release-phase 层唯一开关；默认关闭。

## 7. legacy adapter 政策

`DiagramQualityIssue` / `GraphFinding` / `RuleFinding` 仍是**领域内部模型**，是 adapter 的输入；
对外规则真相只有 canonical 契约。

- parity 测试按**完整关联 tuple** 比较：`(code, severity, sorted(object_ids), sorted(element_ids))`
  —— 分开比较 code 与引用集合会在两个 finding 互换引用时仍然变绿。
- canonical 输出顺序另有独立测试，不与 parity 混为一谈。
- 改变某条规则的**结论**属于规则变更，必须单独评审，不能混在接口重构里。

## 8. 质量分门例外（唯一有意迁移的规则决策）

`QUALITY_SCORE_BELOW_TARGET` 是 M4 中**唯一**把决策搬到 canonical 引擎的规则：门槛一旦可由
项目 profile 配置，判定就必须用**解析后的生效阈值**；否则配置只是展示字段，会形成“看起来生效、
真实门禁仍认 95”的假配置。

- built-in 默认阈值 `95`，与旧制**逐条 parity**（测试锁定）。
- canonical issue 记录生效 `threshold` / `expected` / `actual` / `rule_source`。
- 生产 release 路径不得出现第二个独立的 95 分门。
- 其余 legacy 规则在 M4 内保持 pass-through。

## 9. waiver

- 只在服务端 profile 中声明；Agent 不能自行发放（否则等于变相自批）。
- **永不删除 issue**：issue 留在确定性结果里，带 `waiver_status` 与证据。
- 必需信息：actor、reason、**显式 `granted_at`**（不得在加载时自动生成）、`expires_at?`、
  `max_revision?`、作用域（规则/代码/object/element）。
- 时间必须**带时区**；`expires_at <= granted_at` 视为配置错误。
- 选择算法确定：收集**全部**匹配项 → 有 active 则取 `waiver_id` 字典序最小者，否则取 expired 中
  字典序最小者，否则 `not_waived`。声明顺序不得改变判定或证据。
- fingerprint 覆盖**完整作用域**（含 `object_ids`/`element_ids`）。

## 10. release readiness

- 只输出 `eligible | not_eligible`；**没有任何字段或写路径**能表达 `Approved`/`IFC`/`AFC`/签名/人工批准。
- 必需 validator 未运行或被跳过 ⇒ **fail closed**。
- `ReleasePolicy.fail_on`（默认 `blocker`）决定不合格；被 waiver 的 issue 仍然计数、仍然可见。
- 未注册 code ⇒ fail closed。
- 人工审批永远是独立门（Charter §11）。

## 11. 表面（surfaces）

REST / MCP / CLI / UI 都是**同一个引擎 + 同一个 profile 解析器**的适配层：
同一 document/profile/as-of 输入必须产出同一 canonical payload（只允许表面包装差异）。
禁止四套各自实现策略。校验与就绪检查是**读**：即使记录调用审计，也只记 read/tool 证据事件，
不得因此给文档追加 revision/history。

## 12. 审计与 provenance

- 校验结果是读：运行校验**不产生** `revision.created`。
- 若部署记录 read/tool 调用，审计事件应绑定 canonical result/readiness hash，并明确区别于
  revision 创建与人工批准证据。
- waiver 证据（含作用域）属于 rule-bundle fingerprint 的一部分。
- CAD 导入的源 SHA-256 与解码器证据为**服务端派生**，同时存在于文档 provenance 与审计记录；
  调用方字段不得覆盖保留审计键（`RESERVED_EVIDENCE_KEYS`）。

## 13. 测试与验收

- `backend/tests/test_validation_contract.py`：parity（关联 tuple）、契约、边界（fail closed、
  waiver 可见性与过期、未知规则、时间输入、确定性重复）测试。
- offline quality harness 增加 `validation_contract` 用例（稳定 code 目录快照、adapter 发出的 code
  必须已注册、canonical 必需字段、blocker/waiver/fail-closed、payload 内不得出现批准/放行字段）；
  与既有 7 个用例合计 8/8。
- 代表性大图性能：本机真实图纸 `气路系统总图.dwg`（917 KB / AC1032 / 9283 实体），
  导入一次（converter 时间单列）→ 1 次 warm-up → **5 次**测量 canonical 全量
  （graph + report + drafting），报 5 个原始值 + median/min/max + cold 值，并记录 HEAD/OS/Python/CPU/
  converter 版本/profile 与 fingerprint/符号目录 fingerprint/最终元素数。M4 不设跨机器硬阈值。
- M4-6 全量验收：backend ruff + pytest、quality harness、前端单测、生产构建、Playwright
  （含 shared-mode security acceptance）、真实数据证据、确定性重复证据、Agent 不能自批的负向测试。

## 14. 迁移与债务

- legacy finding 类在 M4 期间可以继续作为领域模型存在，但**任何新消费者不得把它们当作对外契约**；
  删除/改名的清理属于后续工作，除非它导致重复规则真相。
- M4 范围内的 CAD intake 冻结：除正确性/安全/provenance 回归外不扩展。
