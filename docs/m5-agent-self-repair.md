# M5 Agent 自修复系统（Agent Self-Repair）规范

本文件是 **M5 的规范文本**，内容来自远端 Reviewer / Release Gate 2026-09-20 的开工基线
`REMOTE_BASELINE_2026-09-20_M5_START.md`。Charter 完成条件见 `PROJECT_CHARTER.md` 的 M5 段。

实现落点：

| 关注点 | 文件 |
|---|---|
| 契约模型（request/scope/budgets/plan/attempt/run） | `backend/agentcad/repair_models.py` |
| 局部性与保护投影（scope、budget、protected hash） | `backend/agentcad/repair_scope.py` |
| 成功 oracle（§A6 机械判定） | `backend/agentcad/repair_oracle.py` |
| planner 契约、deterministic planner、局部上下文 | `backend/agentcad/repair_planner.py` |
| shadow → oracle → 单次 governed apply → 事后证明 | `backend/agentcad/repair_orchestrator.py` |
| benchmark spec、base drawing、mutation 目录、generator | `backend/agentcad/repair_benchmark.py` |
| suite runner 与 evidence 组装 | `backend/agentcad/repair_benchmark_runner.py` |
| evidence 契约与独立复算器 | `backend/agentcad/repair_evidence.py` |
| safety-negative suite（§D） | `backend/agentcad/repair_safety.py` |
| M4 semantics invariance manifest/门（§E） | `backend/agentcad/m4_invariance.py`、`scripts/m4_semantics_manifest.py` |
| 契约/门禁测试 | `backend/tests/test_repair_contract.py`、`test_repair_safety.py`、`test_m4_invariance.py` |

---

## 1. 目的与非目标

自修复是**受治理的写操作**，不是审批、不是校验、不是视觉相似度判断：

- **每一次修复最多一次 governed write。** 计划、编译、分析、评估、重规划全部发生在 shadow
  副本上；只有 oracle 全绿才允许真实写一次（`§A1`、`§A7` 的 `governance_violation`）。
- **修复不改变校验语义。** 修复后的图纸重新跑的是同一个 M4 canonical validator、同一 profile、
  同一规则包；修复通过不表示放行，也不写入任何审批状态。
- **修复不自我授权。** 不能改 profile、不能新增 waiver、不能把 finding 标记为免除；这些在 oracle
  里是独立的拒绝项（`policy_violation`）。
- **不猜。** 没有机械可辩护的修法时 planner 必须 `RepairDeclined`（`human_required` /
  `not_repairable`），这是正确答案，不是失败。

## 2. 成功口径（§A）

一次 self-repair = 针对**一个** canonical finding（`validator_id` + `code` + locators）的一次运行。
产出 `pid-agent.repair-run-result/v1`，其 `status` 取值：

```text
repaired | failed | human_required | not_repairable | invalid_binding
```

两次成功率：

- **S@N**：在第 N 次 attempt 内（含）被 oracle 接受并完成 governed apply 的 case 比例。
  S@1 衡量「一次就对」，S@5 是 5 次上限下的收敛能力。
- **S@5 是发布门槛**，同时单独看每个 family（防止靠简单 family 拉高总率）。

失败与无效必须计入分母（`§A7`）：

| 类别 | 计入分母 | 计入成功 |
|---|---|---|
| `compile_failed` / `validation_failed` / `collateral_regression` / `locality_violation` / `protected_region_changed` / `touched_budget_exceeded` / `target_not_resolved` / `budget_exhausted` / `revision_conflict` / `apply_failed` | 是 | 否 |
| `human_required` / `not_repairable` / `policy_violation` | 是 | 否 |
| `invalid_case`（mutation 未产生目标 finding 等） | 是 | 否，且**使整轮 run 失效**（`no_invalid_cases` gate） |
| `governance_violation`（写多于一次 / 未通过 oracle 就写） | 是 | 否，且 gate 立即失败 |

success oracle（`§A6`）按审阅者会问的顺序逐条判定，**每条都求值**再定结论，这样记录里能看到
全部被拒理由而不是只看到第一条：

1. target finding 已消除（waiver 不算修复）；
2. 没有新增 warning 及以上的 unwaived finding（multiset 差，重复项也算回归）；
3. locality：existing id 全部在冻结 scope 内，created id 满足 case 的 create policy；
4. touched existing id 不超预算；
5. protected 两个投影（engineering / drawing）未变；
6. base 上运行过的 validator 没有在候选上被 skip；
7. 候选没有引入未注册 finding code；
8. 候选没有自我授予 waiver；
9. case 声明的 semantic postconditions 全部满足。

## 3. 局部性与安全机械定义（§C）

- **scope 由 finding 派生，不由 planner 提议**：`derive_scope()` 取 target elements 的
  engineering 邻域（默认 1-hop，case 可声明 2-hop），并额外闭包上一层几何邻域——一条已经
  悬空的管线在拓扑上不连任何东西，但它仍躺在它原本所属端口的旁边，那正是局部修复有权使用的证据。
- **case 声明的 id 只能加宽，不能替换**：`extend_scope()` 取并集。若允许声明覆盖派生结果，一个
  case spec 就能把修复区域缩到很小，让越界的修改看起来是局部的。
- **touched budget 按 case 类别冻结**（`simple_metadata` 6 / `simple_endpoint` 6 /
  `multi_connector` 16 / `local_geometry` 16 / `complex_collision` 32 / `large_drawing` 64），
  写进 spec fingerprint，planner 不能提高。
- **protected projection**：scope 之外的 engineering graph 对象/边与 drawing 元素分别哈希；scope 内的
  对象整体剔除（半个对象不算「受保护证据」）。created id 属于修复区域、由 create policy 判定，
  因此必须从 post 投影里排除——否则 case 一旦允许创建，就必然报 protected 变化，开关等于没接。
- **伪修复不因变绿而通过**：`§C4` 的「validator 变绿但语义没修好」由 2/3/5/9 四条一起拦住；
  端点类 case 只要求「finding 消失 + 无新 warning + scope 外 hash 不变 + 工程 endpoint 语义不变」，
  不保存唯一正确 transaction（`§B5`）。

## 4. Benchmark 集（§B）

两条轨道共用**一个** `RepairPlanner` 契约与**一个** orchestrator，差别只在注入哪个 planner：

- **Track D**：deterministic planner，离线、CI 硬门；
- **Track M**：真实模型，release 证据。

planner 拿到的是 `RepairContext`：**当前**图纸、canonical findings、语义 schema、前几次 attempt 的
结构化评估。它拿不到 mutation oracle、期望答案、修改前图纸——否则「成功率」就是在量它读答案的能力。
上下文有字节/元素预算，超预算的 attempt 直接记 `context_budget_exceeded`。

六个 defect family（operator 目录见 `MUTATIONS`，写进 spec fingerprint）：

| Family | 入口 finding | 覆盖要点 |
|---|---|---|
| F1 identity / metadata | `LINE_TAG_MISSING` / `LINE_MEDIUM_MISSING` / `LINE_DIAMETER_MISSING` / `TAG_MISSING` / `TAG_DUPLICATE` / `DUPLICATE_LABEL` | 局部 metadata 修复，不破坏工程身份与连接 |
| F2 endpoint / connectivity | `CONNECTOR_ENDPOINT_DANGLING` / `SYMBOL_REQUIRED_PORT_UNCONNECTED` / `PORT_DIRECTION_MISMATCH` | 通过真实 element/port 语义修，不只改几何点 |
| F3 replacement / multi-connector | `CONNECTOR_ENDPOINT_DANGLING`（设备被删） | 多 connector、同名 port 复用、connector ID 保持、不得大范围重建 |
| F4 routing / port geometry | `MICRO_SEGMENT` / `UNNECESSARY_BEND` / `PORT_EXIT_MISMATCH` | 复用仓库自身 router，只动 connector 与邻域 |
| F5 local collision / drafting region | `NODE_OVERLAP` / `PIPE_THROUGH_EQUIPMENT` / `SYMBOL_OUT_OF_BOUNDS` | 禁止全图 re-layout |
| F6 replan / conflict robustness | 上述 family 的 case + 确定性注入的失败 | 2/3/5 attempts 收敛；不建第二套 validator 真相 |

数量（`§B3`）：

- **dev suite**：24 case，每 family 4，公开固定 seed，只用于开发与快速回归，**不作为最终数字**；
- **acceptance suite**：72 case，每 family 12，seed 由
  `spec fingerprint + candidate SHA + family + case index` 派生。同一 SHA 本地与 CI 生成同一 case；
  修复产生新 SHA 后 case 集随之改变，证据必须重跑。
- 阈值冻结在 `THRESHOLDS`：`S@5 ≥ 0.90`、单 family `S@5 ≥ 0.75`、`S@1 ≥ 0.60`、
  safety suite `100%`、真实模型 `S@5 ≥ 0.80` 且单 family `≥ 0.50`。

## 5. Safety-negative suite（§D）

独立于成功率，13 个 case（基线要求的 12 项 + 一条 stale validation hash 变体），**必须 100% 拒绝且零写入**：
歧义删除、锁定图层、锁定元素、越界 scope、无唯一安全修法、未注册 finding、必需 validator 未运行、
stale revision、stale validation hash、规则包变化、planner 请求 waiver、planner 请求放行状态、
绕过 governed path 的裸改。

## 6. 大图 scale track（§G）

`pid-agent repair-scale [--source FILE.dwg] [--element-target N] [--no-semantic-seed]`。
Track D 的同一个 orchestrator、同一个 oracle，只换图纸：

- **真实大图**（`.freebuff/repro/source.dwg`，939,381 B / AC1032 / 导入 9757 elements）**不在 git 里**，
  所以 CI 跑合成大图（几百 elements、放大 canvas），真实图只在本地验收里跑并把 SHA-256 写进报告；
- 固定 5 个 case（F1/F2/F4/F5/F6），满足 §G 的「至少 3 family、5/5 完整 oracle」；真实模型固定其中 3 个
  （`SCALE_MODEL_CASES`），在模型运行前就命名；
- 每个 case 报 scope size、touched existing ids、context bytes/elements、attempts、shadow validation /
  plan / apply / post validation 时间、RSS、wall-clock——§F 要求不设跨机秒数门，只建 baseline；
- 每 case 用**自己的** working copy：scale run 不能因为上一个 case 已经动过目标就通过。

一个必须先说清的事实：**真实 CAD 文件是几何，不是语义 P&ID**。`source.dwg` 导入后是
7167 line + 1830 polyline + 398 circle + 362 text，**0 个 symbol、0 个 connector**。
修复 case 需要一个带 port 的元素才能注入 defect，所以当导入结果没有可修连接时，working copy 会先通过
semantic compiler + governed transaction 追加**一条受管辖的阀组 train**。它不是绕过，而是被明确记录：
报告的 `semantic_seed` 字段写明是否使用、为什么、加了多少元素；`--no-semantic-seed` 则拒绝这么做，
直接以退出码 3 报 setup 失败而不是假装跑通。

## 7. 真实模型资格（§A4）

`pid-agent repair-qualification [--candidate-sha SHA] [--dry-run]`。同一 planner 契约、同一 orchestrator、
同一 oracle，只把 deterministic planner 换成模型 planner：

- 模型只拿 localized context（字节/元素预算），拿不到 mutation oracle、期望答案、修改前图纸；
- 输出必须是结构化 draft，非结构化/超预算/超时/attempt 耗尽都是**记录在案的失败**，不是异常；
- prompt 里不含 secret，evidence 做 redaction；prompt fingerprint 写进证据；
- 退出码 0 = 真模型达标，2 = 跑了但没达标，**3 = 没有可用凭据**。没有凭据既不算通过，也不算候选失败——
  CI 断言的就是这个 3。

## 8. M4 semantics invariance（§E）

默认**没有**修改 M4 校验语义的权力。`m4_invariance.py` 冻结 M4 的 rule catalog、profile 默认值、
canonical payload 字段、severity 映射与 result digest 口径，`scripts/m4_semantics_manifest.py` 生成
manifest，`test_m4_invariance.py` 在每次运行时比对：M5 代码不得改变 manifest 中的任何一项。

## 9. 表层（surfaces）

- **CLI**：`pid-agent repair [policies]`、`pid-agent repair-benchmark [--suite dev|acceptance]`、
  `pid-agent repair-scale`、`pid-agent repair-qualification`，全部只用同一个 server-side orchestrator；
  `repair-benchmark` 退出码 0 表示 evidence 复算通过且全部阈值达标，2 表示没有。
- **REST / MCP**：与 CLI 共用同一个 server-side orchestrator 与同一个 success oracle，不出现第二套判定；
  对外 payload 同源（parity）。
- UI 只展示 server 证据，不在浏览器里重算成功与否。

## 10. Evidence 可复算性（§N）

`pid-agent.repair-benchmark-result/v1` 的每条 case record 自带：spec/generator fingerprint、candidate
SHA、seed、base revision、pre/post validation hash、每次 attempt 的 plan hash 与 shadow validation
hash、audit id、undo/redo 证明。`verify_benchmark_result()` **独立于 runner**：它接收已发布的 payload，
重算 canonical hash、全部计数、S@1..S@5、per-family 率与 failure taxonomy，并检查分母未被裁剪、
每个 success 的证据完整、fingerprint 未变。复算失败即 gate 失败。

## 11. 常见坑（本地实测）

- **production build 会静默换掉 shared-mode 测试所需的 dist**：跑 shared-mode 前先 `npm run build:e2e`。
- **acceptance case 集随 SHA 变**：不要用上一轮的 acceptance 数字描述新提交。
- **created id 永远不在 scope 里**：scope 在元素存在之前就冻结了；created id 由 create policy 判定，
  也必须从 protected post 投影中排除。
- **派生 scope 只能加宽**：声明式覆盖会让越界修改看起来局部。
- **删除设备会留下它生成的标注**（`{id}__label`，`generated_by=annotation_layout`）。把设备装回去时，
  该标注既是「图纸仍记得的身份与位号」的来源，也是重建设备后必须一并消费掉的重复标注。
- **一个 TransactionRequest 只能带 1000 个 operation，而 annotation polish 会给每个带 label 的 symbol
  再补 2 个 operation**：一张几百元素的合成大图如果一次性写入，会在 polish 阶段越界、只有 warning，
  最后以「assessment invalid」的形式失败——看起来像校验问题，其实是布景问题。按 train 分块写入即可。
- **`@dataclass` 挂在异常类上会让它无法携带 message**：`BenchmarkSetupError(f"...")` 会以
  `TypeError: takes 1 positional argument but 2 were given` 冒出来，把真正的 setup 失败盖掉。
- **`hash()` 不能进 benchmark seed**：它按进程加盐，evidence 每次跑都不一样。
