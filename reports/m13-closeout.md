# M13 Closeout — Governed Multi-Domain Change Sets & Impact Analysis

- 状态：等待 Gate 签署（PR #86 exact-head MERGE GO + M13-D5 CLOSED 后生效）
- main 基线：D1~D4 已合入（#82 #83 #84 #85 → main@b4cdd0b）；本报告覆盖 D5（#86, head=0c468fa）及 M13 整体七项冻结完成标志
- 治理声明：本 closeout 仅针对 M13（Charter v1.2.0 §55C）。WS3B / M9 Closeout / M10 FINAL ACCEPTANCE 维持 Owner 挂起，DEPLOY 未授权；本报告不构成其收口。

## 一、§55C 七项完成标志逐项证据

### 1. Project Change Set Contract — DONE
`project_change_set.ChangeSetIntent` 绑定 change_set_id / project_id / base_member_pins / evaluation_as_of / ordered mutations / canonical intent hash（`canonical_change_set_intent` + `change_set_intent_hash`）。D3 修正后 affected_objects = 直接触及对象 ∪ 经 existing active links 的一跳对端。证据：PR #84 + `tests/test_m13_d3_change_set.py`。

### 2. Deterministic Cross-Domain Impact Analysis — DONE
`ChangeSetImpactAnalyzer` 基于 M12 membership + engineering links + 域语义确定性计算；无名称相似 / LLM 猜测 / fuzzy match；未知对象 / 未知 kind fail closed（`impact_unknown_object` / `impact_unknown_kind` → 422）。证据：PR #84，两组语义场景测试。

### 3. Multi-Domain Shadow Preview — DONE
`ChangeSetPreviewer` 零工程写入输出 per-domain semantic diff、revision projection、link impact、readiness delta、validation result。D5 把 stage 端点冻结为「preview 期间零工程写」并经 e2e 断言（stage 前后 pid revision / cable revision / link pins 不变）。证据：PR #84 + `tests/test_m13_d5_governed_api.py::test_stage_runs_shadow_preview_with_zero_engineering_writes` + `e2e/change_set.spec.ts`。

### 4. Exact Intent Approval Binding — DONE
复用 M10 session/permission/approval；approval 绑定 project_id + declared pins + evaluation_as_of + ordered mutations + D3 canonical impacted/re-pins + effective profile fingerprint；apply 重算 hash 比对，drift → `change_set_conflict` / `change_set_not_current`，零写入；replay → `change_set_not_authorized` 且不覆盖 terminal 成功记录。证据：PR #85，`tests/test_m13_d4_executor.py`（含 C4、replay、冲突、竞态、部分提交回滚六个负例）。

### 5. Atomic Governed Multi-Domain Commit — DONE
一个 change set 一个用户级动作：单事务内 pid CAS + cable CAS + link re-pin + applied 迁移 + evidence + closeout + exactly-one applied 审计；任何失败 = 全回滚 + 独立 closeout（refused + failed tool call/session + exactly-one rejected 审计，无域残留）；losing closeout 对 terminal 记录纯 no-op。禁止两个现有 write endpoint 冒充原子性——executor 是唯一提交路径。证据：PR #85 + TERMINAL-CLOSEOUT FINAL FIX（Gate 已复核签署）。

### 6. Post-Commit Project Evidence — DONE
确定性绑定 before/after pins（含未触及成员，D85-4）、per-domain diffs、link snapshot、readiness/result hash（in-transaction snapshot == post-commit canonical hash，D4 硬锁）、audit references。result_pins == 当前项目 pins → 直接进 M12 deterministic package（e2e 用 evidence.after_pins 取包并与 fresh-process rebuild 字节比对 + `verify_project_package`）。仍只 eligible/not_eligible。证据：PR #85 + `e2e/change_set.spec.ts`。

### 7. Frozen Two-Domain Acceptance + Full Non-Regression — DONE
- 全链场景：e2e `change_set.spec.ts` 真实双域（pid update_element + cable gauge 修改）走通 stage（零写预览）→ governed apply → evidence/readiness hash 一致 → deterministic package 字节一致。
- 负例：apply 非 staged 409、shared mode 403 fail-closed、非法 intent 422、approval replay、revision race、partial-commit、membership-link drift（D1~D4 测试矩阵）。
- 非回归：PR #86 CI run 37251474793 四 job 全绿——Backend 1842 passed in 204.32s；Frontend Node 24 pass；Browser local 69 passed + shared 4 passed；M5 self-repair 72-case gate pass。
- M12 package / P&ID / Cable 域测试零退化（1842 全绿内含全部历史套件）。

## 二、性能裁定（Gate-approved 证据法：同 workflow 同 runner exact log 对比）

| 证据 | 数值 |
| --- | --- |
| 冻结基线（M13-D2 时，run 37210734487） | 1809 passed / 260.37s |
| 预算规则 | ≤ 基线 +10%，ceiling = 286.407s |
| D4 末次（run 37248173673） | 1836 passed / 280.61s（+7.8%，PASS） |
| **D5 末次（run 37251474793，本 PR head）** | **1842 passed / 204.32s（−21.5% vs 基线，PASS）** |

D5 本地增量（5 个 API 测试 + surface contract 声明）极轻，CI 波动范围内大幅低于天花板。裁定：**M13 closeout 性能 PASS**。

## 三、M13-D5 交付物清单（PR #86, head=0c468fa）

- `POST /api/v2/projects/{pid}/change-sets`：sealed D3 analyzer + zero-write shadow previewer → D2 primitives 落 staged 行；一条 governance-plane 审计（`project_change_set.staged`）。
- `POST .../change-sets/{cid}/apply`：只路由进 sealed M10/D4 flow（session→approval→authorization→atomic executor），零复制域逻辑；local 可自动批准，shared fail closed（403 `approval_not_self_served`）。
- GET read surface（#86 前一支 a60da9f）：list/detail 暴露全部 durable facts，零审计。
- surface_contract 声明两条写路由（governance_write + audited）；审计 Literal 增 `project_change_set.staged`。
- UI：ProjectPanel 变更集只读区（id/status/updated_at/evidence readiness hash + result pins），未新增 domain tab（视觉基线不动）。
- e2e：`change_set.spec.ts`（全链 + 字节 parity）+ `security.shared.spec.ts` 匿名 401 扩展。
- 后端测试：1842 passed；ruff 净；前端 163 单测 + build:e2e 绿。

## 四、遗留与边界

- readiness 仍只出 eligible/not_eligible，不产生 Approved/Released（§55C-6 边界保持）。
- 多项目 UI、第三域、change-set undo/redo 的跨域组合为后续 milestone 范围。
- WS3B / M9 Closeout / M10 FINAL ACCEPTANCE：Owner 挂起，设待办，待 Owner 决定。
