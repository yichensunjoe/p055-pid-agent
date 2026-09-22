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
| coverage ledger 与晋升证明（§9） | `backend/agentcad/repair_coverage_ledger.py` |
| 晋升前后投影证据 | `scripts/m5_promotion_projection.py`、`reports/m5-promotion/**` |
| M4 semantics invariance manifest/门（§E） | `backend/agentcad/m4_invariance.py`、`scripts/m4_semantics_manifest.py` |
| 契约/门禁测试 | `backend/tests/test_repair_contract.py`、`test_repair_safety.py`、`test_m4_invariance.py`、`test_repair_coverage_ledger.py` |

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

成功率：

- **S@N**：在第 N 次 attempt 内（含）被 oracle 接受并完成 governed apply 的 case 比例。
- **S@1…S@4 是观测指标，不是拒绝门**：suite 里存在**契约上就要求重试**的 case（F6 的故障注入
  让第 2/3/5 次才可能成功），对它们 S@1 必然为 0。用 S@1 拒绝就等于在度量 fixture 而不是 planner。
  这四个数字照常发布，供人对比不同 planner。
- **发布门槛是三条**：`S@5 ≥ 0.90`、单 family `S@5 ≥ 0.75`、以及 **F6 自己的 `S@5 = 1.00`**
  （F6 存在的意义就是证明“在注入失败下最终收敛”，所以不能只靠 family 最低线蒙过去）。
- 另外每条 case 带**attempt 契约**：声明需要 N>1 次的 case，必须**恰好在第 N 次**第一次成功——
  这同时证明了注入真的发生了、且 planner 确实需要那些尝试。未声明契约的 case 不对 attempt 数作任何声明。

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

**这张表是 family 的“职责范围”，不是“benchmark 能制造的缺陷”。** 两者今天不相等，差距必须写在明处
（`f1_symbol_tag_missing` / `f1_symbol_tag_duplicate` 就是因为这个差距被补上的：规则修好之前，
F1 表里写着 `TAG_MISSING`/`TAG_DUPLICATE`，但目录里没有任何 operator 能制造它们）：

- **目录能制造（23 operator / 17 code，acceptance 里出现 17 个非空 code；与
  `supported_code_manifest()` 一致）**：上表 F1–F5 中
  `LINE_TAG_MISSING`、`LINE_MEDIUM_MISSING`、`LINE_DIAMETER_MISSING`、`TAG_MISSING`、`TAG_DUPLICATE`、
  `DUPLICATE_LABEL`、`CONNECTOR_ENDPOINT_DANGLING`、`SYMBOL_REQUIRED_PORT_UNCONNECTED`、
  `PORT_DIRECTION_MISMATCH`、`MICRO_SEGMENT`、`UNNECESSARY_BEND`、`PORT_EXIT_MISMATCH`、
  `UNBRIDGED_CROSSING`、`NODE_OVERLAP`、`PIPE_THROUGH_EQUIPMENT`、`SYMBOL_OUT_OF_BOUNDS`、
  `ANNOTATION_OVERLAP`。
  （后四项由 **coverage promotion** 从 coverage-extension track 晋升而来：spec v3 / corpus v3，见 §9。）
- **有修复策略、但**无法被任何受支持入口制造**：`SYMBOL_DEFINITION_MISSING`、
  `CONNECTOR_ENDPOINT_PORT_MISSING`、`CONNECTOR_ENDPOINT_POINT_MISMATCH`。这三个 code
  在 `repair_planner.py` 里有策略、在冻结的 rule catalog 里是正式规则，但**写路径与导入路径都不
  允许这样的文档存在**，而且两者拒绝的方式不同（
  `repair_coverage_ledger.probe_representability()` 逐个记录）：

  | code | governed write | import |
  |---|---|---|
  | `SYMBOL_DEFINITION_MISSING` | 拒绝：`unknown symbol: …` | 拒绝：`unknown symbol: …` |
  | `CONNECTOR_ENDPOINT_PORT_MISSING` | 拒绝：`unknown port '…' for symbol …` | 拒绝：`unknown port '…' for symbol …` |
  | `CONNECTOR_ENDPOINT_POINT_MISMATCH` | **接受并重算**：写入成功，绑定点被 `_normalize_endpoint` 重新导出，缺陷从不出现 | 拒绝：`… binding point is stale` |

  所以“benchmark 考不到它们”不是遗漏，而是产品自身的入口把它们挡在门外：规则是给**其它来源**
  （旧库、外部生成的文档、将来第三方生产者）留的纵深防御，planner 也照样保留策略。
  这三个 code 已按规定进 disposition ledger（`repair_coverage_ledger.UNREACHABLE_ENTRIES`，
  并在 `ledger_manifest()` 里发布），`test_unreachable_codes_cannot_be_staged_by_any_supported_surface`
  是这条断言的守卫：哪天某个面开始接受它们，测试就红，必须把它们提升成正式 case。
  在补上真正的 producer 之前，任何“F1/F2/F4/F5 覆盖了这三个 code”的说法都是错的。
- **上表出现但既无 operator 也无策略**的 entry finding，只表示该 family 的入口语义，不代表被测量。

数量（`§B3`）：

- **dev suite**：24 case，每 family 4，公开固定 seed，只用于开发与快速回归，**不作为最终数字**；
- **acceptance suite**：72 case，每 family 12，seed 由
  `spec fingerprint + candidate SHA + family + case index` 派生。同一 SHA 本地与 CI 生成同一 case；
  修复产生新 SHA 后 case 集随之改变，证据必须重跑。
- 阈值冻结在 `THRESHOLDS`（spec **v3**）：`S@5 ≥ 0.90`、单 family `S@5 ≥ 0.75`、
  `f6_s5_overall = 1.0`、safety suite `100%`、真实模型 `S@5 ≥ 0.80` 且单 family `≥ 0.50`；
  另有非阈值门 `attempt_contract`（见上文）。**全局 `S@1` 已从硬门中移除**（v1 曾为 `≥ 0.60`）。

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

## 9. Coverage ledger 与 coverage promotion（review §③）

`pid-agent repair-coverage [--database PATH] [--output PATH]`，实现于 `repair_coverage_ledger.py`。
它回答一个比“成功率多少”更早的问题：**修复策略里认识、但当初的冻结语料造不出来的那些 code，
最后各自去了哪里？**

这个 track 分两步走。第一步（coverage-extension）是**发现**：对 7 个“有策略、无 operator”的 code
逐个做机械判定，证明 4 个能被这个仓库的文档持有、3 个不能。第二步（**coverage promotion**，
远端 2026-09-21 签的 Gate）是**结算**：4 个能持有的晋升为冻结语料的正式 case，3 个不可达的进
disposition ledger。本模块现在是第二步的记录，而且是一份**检查**而不是一份说明。

### 9.1 已晋升的 4 个 code（spec v3 / corpus v3）

`DUPLICATE_LABEL`、`PORT_DIRECTION_MISMATCH`、`UNBRIDGED_CROSSING`、`ANNOTATION_OVERLAP` 各有一个
确定性 producer，写路径是 `apply_transaction`，现在注册在冻结目录里：

| code | operator | family | 底座 |
|---|---|---|---|
| `DUPLICATE_LABEL` | `f1_duplicate_label` | F1 | `three_valves` |
| `PORT_DIRECTION_MISMATCH` | `f2_port_direction_mismatch` | F2 | `three_valves` |
| `UNBRIDGED_CROSSING` | `f4_unbridged_crossing` | F4 | `three_valves+crossing` |
| `ANNOTATION_OVERLAP` | `f5_annotation_overlap` | F5 | `three_valves` |

晋升不是把 producer 挪个位置就完事。`prove_promotion()` 对每个 code **从冻结 generator 里取它那一条
acceptance case**（`promotion_cases()`：同 family、同 candidate SHA、同样的 round-robin 规则），
要求 canonical validator 在同一图纸上比对改动前后、声明的 exact code *真的出现*（不看 operator 名字），
再交给与冻结语料**完全相同**的 runner / orchestrator / oracle。当前 4/4 `covered`：
`case_is_success()` 复算通过、单次 governed write、protected 前后一致。

- **case 是推导出来的，不是手挑的**：如果晋升被回退、或 operator 从 family 轮转里掉出去，
  `promotion_cases()` 直接失败，而不是继续报告一份语料其实没有的覆盖。
- producer 顺带引入的其它 finding **不会被隐藏**：proof 里的 `added_codes` 就是“这次改动多出了哪些 code”。
  目前只有 `QUALITY_SCORE_BELOW_TARGET`（打分规则看整张图）与 `UNBRIDGED_CROSSING` case 的
  `EXCESSIVE_BENDS`（两个端口都是水平朝向时，任何“穿过横管再回到端口”的正交路径都至少 4 个拐点）。
  两者都在**变异后的底座**里，因此不是 oracle 意义上的 regression；写出来是为了让审阅者能自己判断。
- **staging 语料已退休**：原来的 `m5-coverage-extension`（corpus v1、4 case）不再存在——留着它会让两个
  语料同时声称这 4 个 code。它当初发布的身份（`coverage_corpus_digest = 3dc8ca1a…`、
  `report_hash = f56c2c50…`、两个解释器各自的 `coverage_fingerprint`）记在
  `SUPERSEDED_EXTENSION_IDENTITIES` 里：**记录，不重算**，因为产生它们的代码已经变了。

### 9.2 不可达的 3 个 code 与其正式 disposition

`probe_representability()` 在**写面**与**导入面**各试一次，记录“拒绝原文”与“是否真的留下缺陷”两件事。
三个 code 的结论不同，这本身就是发现：`CONNECTOR_ENDPOINT_POINT_MISMATCH` 的写请求**会成功**
（`accepted=true`），但绑定点被 `_normalize_endpoint` 重算，缺陷从不落地——只断言“没抛异常”的测试会把它
误判成可达。远端把它固定成正式 disposition，而不是继续叫“漏测”：

| code | disposition | governed write | import |
|---|---|---|---|
| `SYMBOL_DEFINITION_MISSING` | `unreachable_at_supported_ingress` | 拒绝：`unknown symbol: …` | 拒绝：`unknown symbol: …` |
| `CONNECTOR_ENDPOINT_PORT_MISSING` | `unreachable_at_supported_ingress` | 拒绝：`unknown port '…' for symbol …` | 拒绝：`unknown port '…' for symbol …` |
| `CONNECTOR_ENDPOINT_POINT_MISMATCH` | `normalized_or_rejected_at_supported_ingress` | **接受并重算**：写入成功，绑定点被 `_normalize_endpoint` 重新导出，缺陷从不出现 | 拒绝：`… binding point is stale` |

两个名字必须分开：`unreachable` 表示入口**拒绝**这种状态，`normalized_or_rejected` 表示写入**成功但被重算**
（或导入被拒）。合并成一个词就会把这条发现藏起来。rule 与 planner 策略保留作纵深防御；
`test_unreachable_codes_cannot_be_staged_by_any_supported_surface` 是守卫：任何受支持入口哪天开始允许
这些状态存在，测试立刻红，届时**要把它们提升成正式 case**，而不是静默改变结论。store 层的 validator
单测可以有，但它属于内部 validator coverage，不得伪装成 repair benchmark 的 producer。

### 9.3 身份、退出码与边界

- ledger 有自己的身份 `ledger_id = m5-coverage-ledger`、`ledger_version = 1`。
  `ledger_digest()` 是**跨解释器恒定**的那一半（manifest 去掉 `ENVIRONMENT_DERIVED_KEYS`），
  `ledger_fingerprint()` 是包含 `generator_fingerprint` 的完整发布值——后者摘要的是 CPython 字节码
  （`co_code`），换个解释器就不一样，所以“冻结”必须是**语料**冻结，不能是“在这台机器上冻结”。
  `tests/test_repair_coverage_ledger.py` 里有一条不需要第二个解释器就能证明这个性质的测试：
  把 `generator_fingerprint` 换成另一个运行时的值，digest 不动、fingerprint 动。
- 冻结语料自己有 `corpus_id = m5-core-corpus`、`corpus_version = 3`、`case_count = 72`、
  `operator_count = 23`、`supported_codes`（`core_corpus_manifest()`）。
  **两者分开指纹**：增加覆盖不该让「M5 当时到底验收了什么」变得无法回答。
- **语料身份现在是两层（A5）**：
  - `core_corpus_digest()` = **跨解释器恒定**的语料身份。输入是 `core_corpus_projection()` —— 布局、
    case 派生规则、**完整 operator 目录**与**两个 suite 的每一条 case**，而不是它们的计数（`case_count`、
    `operator_count` 这类摘要看得见“有多少”，看不见“哪一条由谁画、以什么声明画”）。
    **加上 safety-negative suite 本体**（`safety_case_universe`：13 条 case_id + title + builder 定义身份；
    source 字段就写 `declarative table in agentcad/repair_safety.py`）——§D 是 100% 要求，而原先只有
    `safety_case_count` 这个计数，计数看不见一条 case 被增删/改题/换实现。v3 golden `96b999fa…`。
    `core_corpus_digest_manifest()` 就是这份 projection 本身，供人复算。
  - **一个数字上的不一致必须拆开、不能混成一个字段**：spec 里 `safety_cases = 12`，而
    `repair_safety.SAFETY_CASES` 实际有 **13** 条（`d8b_stale_validation_hash` 是后加的；acceptance 门槛一直
    按真 suite 跑 13/13）。那个数在冻结 spec body 里，改了会动 `spec_fingerprint`，所以投影里
    **不再有裸名的 `safety_case_count`**，改成五个字段：
    ```
    status                          : "projected" | "unavailable_archived"
    spec_declared_safety_case_count : 12          # 冻结 spec 发布的历史值
    actual_safety_case_count        : 13          # == len(cases)，恒等，不能手写
    cases                           : [13 行] | null
    count_disposition               : {declared: 12, actual: 13,
                                       status: "frozen_spec_metadata_mismatch"}
    ```
    `actual` 必须等于 `cases` 的长度（测试里是硬断言），所以“改了 suite 却留着旧计数”直接 fail；
    两个数不同时由 `count_disposition` **点名**而不是静默；下一次真正切 spec 版本时必须消掉这个 mismatch。
  - 归档语料投不出这 13 条（v2 spec body 只记了 declared count），所以它给的是 `status = unavailable_archived`、
    `cases = null`、`actual = null`——**null 而不是 `[]`**，因为空列表会被读成“v2 没有 safety case”。
  - **identity 的敏感性与稳定性是一对**：`tests/test_core_corpus_identity.py` 里 8 个反向 mutation
    必须移动 digest（改 operator_id / 交换两个 id / 改 defect code / 改 base_variant / 改写策略 /
    换 producer 实现 / 增删一条 case / 增删一个 operator / 改 seed 派生规则），3 个正向稳定性必须不动
    （`generator_fingerprint` 变、`spec_fingerprint` 变、解释器变）。两组合起来才定义了身份的边界。
  - `core_corpus_digest("2")` = 归档语料的同一身份，`edb1c5d3…`（从冻结的 v2 spec body 重放：19 operator /
    72 case；实现那半边记为 `ARCHIVED_DEFINITION_IDENTITY`，safety 本体记为 unavailable）。它是 **A5 之后派生出来的
    archival 身份**，v2 从未发布过这个字段，也不声称能证明当年的 Python 实现字节。
  - **定义身份的“名字”必须从源码读，不能从编译器读**：第一版用 `inspect.getclosurevars` 决定哪些名字是全局，
    于是同一个函数在 3.11 把 `__name__` 算成已解析的全局、在 3.12 不算（3.12 把推导式内联了，`co_names` 变了），
    identity 仍然会随解释器变——而它只会在**第二个解释器**上暴露（本机 3.12 全绿、3.11 挂 5 条）。
    现在名字来自 AST（`_referenced_names`），分类来自 `co_freevars`/`__closure__`，值来自模块命名空间：
    同一份源码在两台机器上得到同一组候选名。
  - **producer 的声明式那一半不够时改指纹什么**：目录行额外发布
    `producer_definition_identity` / `base_builder_definition_identity`——源码 AST 的**规范化投影**
    （`agentcad/source_identity.py`）。不能用 `ast.dump`：同一份 `_patch` 在 3.11 是 `930398d67e9220f4`、
    3.12 是 `a182c517faab2e95`，因为 3.12 给 `FunctionDef` 加了 `type_params` 字段。
    规范化投影丢掉 `None` 与空字段，因此新解释器新增的字段只要还是空的就不参与身份。
  - 被调用到的同模块函数会一并折入（producer 不能在调用方没变的情况下在下面改变），工厂产出的
    operator 的绑定参数也计入（`_build_f6` 的 `failures` / `base_operator_id`）。
  - `core_corpus_fingerprint()` = **未重定义**、未删除，仍是“这份定义 + 这份代码 + 这台机器”的 provenance：
    本机 3.12 `c85995d2…`、3.11 `82b37b04…`，两者不同是预期行为。
  - 两个 golden 钉在 `tests/test_core_corpus_identity.py`；`reports/m5-closeout/corpus-identity.*` 是证据。
  - **跨解释器不是断言，是实测**（两处）：
    ① identity 的输入以纯 JSON 发布（`corpus-identity-inputs.json`），
    `scripts/m5_closeout_identity_311_check.py` 只用标准库 + `agentcad.source_identity`（该模块刻意
    零依赖，就是为了能在第二个解释器上跑）复算 digest 与 **两个源码文件里全部 101 条**定义身份
    （`repair_benchmark` + `repair_safety`，键是 `module:qualname`——safety builder 的帮助函数在它自己那个文件里），
    在 CPython 3.11.15 与 3.12.12 上都等于记录值；② 钉住 golden 的那 27 条身份测试本身在 3.11 与 3.12 上各跑一次
    都要绿（本轮就是这么发现上面那个 `getclosurevars` 坑的），CI 的 M5 job 是 3.11，所以“两个解释器给出同一个数”
    由 CI 每天重跑而不是只在本地成立。
  - v2 的 manifest 里**没有** `generator_fingerprint`：v2 operator 的字节码随代码一起消失了，
    该字段无法重算，所以它被“缺席”而不是被伪造（这也正是身份要独立于指纹的原因）。
- `BENCHMARK_SPEC_VERSION` 从 2 切到 **3**，`spec_fingerprint` 从 `c8520c5e…` 变为 `8f522c75…`。
  变的只有“语料能制造哪些缺陷”：阈值、oracle 版本、family 列表、case 布局（12×6）与 safety suite
  全部未动。因此 v3 的重跑仍是 **72/72、S@5 = 1.0、六 family 各 1.0、八项 gate 全绿**，
  S@1 仍是 0.8333（缺口依旧 100% 来自 F6 的注入契约，不是新 case 带来的）。
- **S@1 的缺口是一个被检查的断言，不只是一个观察值**（B 项第一步）。把 72 条 case 逐条摊开：
  S@1 = 0.8333 的缺口**恰好是 12 条 F6 case**，`required_attempts` 分别为 2/3/5（`F6_CONTROL_FLOW` 的三个
  operator 各 4 条），F1–F5 全部首轮通过；也就是说缺口 **100% 由声明产生**（已核实与文档记录一致）。
  之前的 `attempt_contract` 门只检查**声明方向**（声明了 N 次就必须首次成功在第 N 次），
  而“没声明重试的 case 一定首轮成功”这半个断言**从没被检查过**——它一旦逐条退化，上面的每个数字都仍然为真。
  现在 acceptance 回归测试同时钉：① 不带重试契约的 case 首轮必须成功；② 带重试契约的 case 只能是 F6，
  且其 `required_attempts` 必须等于冻结 spec 里该 operator 声明的值（所以“F6 变容易了”会表现为 spec 变更而
  不是分数变好）；③ `S@1` 必须等于“无重试契约 case 数 / 总数”。反向断言的“会响”由 synthetic case 单独证明
  （`test_the_converse_contract_flags_a_first_attempt_a_case_never_declared`）。

  结论：**B 项的 S@1 缺口没有可“修”的 planner 缺陷**——它是 F6 控制流夹具本身；可做且应该做的是
  把它从“一个数”变成“一条会红的契约”。
- **v2 的 spec body 被归档**（`_SPEC_V2_OPERATORS` + `_archived_spec_payload()`），所以
  `spec_fingerprint("2")` 仍能算出发布过的 `c8520c5e…`，`archived_operator_catalogue("2")` 还能把
  v2 的**轮转顺序**也重现出来：旧用例集是推导出来的，不是从存档 JSON 里猜出来的。
  归档表被 `test_the_published_v2_spec_is_still_recomputable` 与
  `test_the_published_v2_case_set_is_still_derivable` 钉住，改动它会立刻红。
- 已发布的 `reports/m5/**`（v2 的 acceptance 证据、extension 证据、qualification）**不重写**；
  本轮的新证据放在 `reports/m5-promotion/`（spec/case 投影 + v3 acceptance + disposition ledger）。
- `verify`/退出码：`repair-coverage` 退出 0 仅当「晋升的 code 全部 covered」+「声称不可达的 code 仍不可达」
  +「四个对照全红」三者同时成立（由 `assert_ledger_is_sound()` 判定）；否则 2。任何一条变松都会让命令
  变红，而不是静默降级。

负向能力（review §③ 的“gate 必须能变红”）：`run_negative_controls()` 固定四个对照，
每个都故意打断链条中的一环，并要求报告里出现预期的信号：

| 对照 | 预期信号 |
|---|---|
| producer 只写不造缺陷 | `not_manifested`（不因“写入成功”而计入覆盖） |
| producer 真造了缺陷但声明错误的 code | `not_manifested`（按 exact code 判，不看名字） |
| planner 修好 target 后越 scope 改一个中性字段 | `locality_violation` |
| planner 直接删掉 target 元素 | `deletion_not_permitted`（§C4 伪修复） |

`verify`/退出码：`repair-coverage` 退出 0 仅当「支持的 code 全部 covered」+「声称不可达的 code 仍不可达」
+「四个对照全红」三者同时成立；否则 2。任何一条变松都会让命令变红，而不是静默降级。

## 10. 表层（surfaces）

- **CLI**：`pid-agent repair [policies]`、`pid-agent repair-benchmark [--suite dev|acceptance]`、
  `pid-agent repair-scale`、`pid-agent repair-coverage`、`pid-agent repair-qualification`，
  全部只用同一个 server-side orchestrator；
  `repair-benchmark` 退出码 0 表示 evidence 复算通过且全部阈值达标，2 表示没有。
- **REST / MCP**：与 CLI 共用同一个 server-side orchestrator 与同一个 success oracle，不出现第二套判定；
  对外 payload 同源（parity）。
- UI 只展示 server 证据，不在浏览器里重算成功与否。

## 11. Evidence 可复算性（§N）

`pid-agent.repair-benchmark-result/v1` 的每条 case record 自带：spec/generator fingerprint、candidate
SHA、seed、base revision、pre/post validation hash、每次 attempt 的 plan hash 与 shadow validation
hash、audit id、undo/redo 证明。`verify_benchmark_result()` **独立于 runner**：它接收已发布的 payload，
重算 canonical hash、全部计数、S@1..S@5、per-family 率与 failure taxonomy，并检查分母未被裁剪、
每个 success 的证据完整、fingerprint 未变。复算失败即 gate 失败。

### 11.1 两个哈希：legacy 与 semantic（远端裁决 ⑥-2）

payload 上同时发布两个结果哈希，用途不同：

- `benchmark_result_hash`（**legacy**）：原来的那个。它经 `repair_digest` → `canonical_digest` 计算，而
  `exclude` 交给 pydantic 的 `model_dump(exclude=…)`，**只删顶层键**；volatile 事实（latency、timings、
  document id、运行时 validation hash 等）藏在 `cases[i]` 里，于是进入哈希。实测同一 candidate SHA 连跑两次，
  `counts`/`s_at`/`gates`/每条 case（剥掉 volatile 后）**逐字段相同**，哈希却不同（本地 `2d827c1e…` /
  `e98863ef…`、CI `36a2383e…`）。**保留不动**：已发布的那三份 acceptance evidence 不重写，
  它们的值仍然可被复算成当初发布的值（v2 的 spec body 也已归档，见 §9.3）。
- `benchmark_semantic_hash`（**semantic**）：结果身份。`repair_semantic_digest()` 递归剥掉
  `REPAIR_SEMANTIC_EXCLUDED_FIELDS`（volatile 字段 + 两个哈希 + `generator_fingerprint` +
  `semantic_hash_version` + 语料坐标），再对 canonical JSON 取 SHA-256；契约版本发布在
  `semantic_hash_version`（当前 `REPAIR_SEMANTIC_HASH_VERSION = "1"`）。同样三次运行得到同一个值。
- **语料坐标是派生元数据，不进任何一个哈希**：A5 起结果 payload 发布 `corpus_version` 与
  `core_corpus_digest`（`core_corpus_fingerprint` **故意不发布**：它按解释器变，放进去会被误读成结果身份）。
  两个名字同时进了 `REPAIR_SEMANTIC_EXCLUDED_FIELDS` 与 `REPAIR_LEGACY_HASH_EXCLUDES`，所以
  “A5 之前发布的 v3 payload”与“同一结果加上两个字段”算出的 legacy 与 semantic 哈希**完全相同**：
  冻结的 `reports/m5-promotion/acceptance-v3.json` 实测 legacy `17be0e45…`、semantic `530e56f1…` 两侧一致。
  真语料变了，case 记录本身（operator、target code、结论）就会让哈希变；不需要派生字段再变一次。

为什么去掉 `generator_fingerprint`：它摘要的是 **CPython 字节码**，同一份语料/结果换解释器就变——这与 §9 里
`ledger_digest` 的口径是同一条规则（“语料身份跨解释器恒定，运行时指纹只作 provenance”）。
`spec_fingerprint`、`candidate_sha`、`oracle_version`、每个 case 的结论与每个 gate **留在**哈希里。

边界：

- semantic 哈希**不算 M4 的** `canonical_digest`：那个规则服务 M4 provenance，不动它；repair 层是新增一条更深的规则。
- **case 顺序属于协议语义**：case 列表是序列不是集合，重排即另一个结果（因此纳哈希）。
- 未知的 `semantic_hash_version` 会在 `verify_benchmark_result()` 里报 `semantic_hash_version_unknown`，不静默比较。
- 待下一次正式 promotion 时：`benchmark_result_hash` 降级为 deprecated，semantic 哈希成为唯一结果身份。

## 12. 常见坑（本地实测）

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
- **“写请求没报错”不等于“缺陷被制造出来了”**：`_normalize_endpoint` / `_prepare_element` 会接受
  一个 stale binding 或未知 port，然后把它重算/拒绝掉。判断一个 defect 是否真实存在的唯一可靠方式是
  **用 canonical validator 读回图纸**，而不是看写调用是否抛异常（`probe_representability()` 因此记录
  `accepted` 与 `defect_present` 两个字段）。
- **同一个模块里 `from x import _symbol` 会被本模块自己的 `_symbol` 覆盖**：coverage-extension 里
  重名私有 helper 导致 `TypeError: _symbol() got an unexpected keyword argument`；导入时显式起别名。
- **“冻结”的哈希不能把“跑它的机器”算进去**：`generator_fingerprint` 取的是 CPython 字节码，于是 CI 的
  3.11 与本地 3.12 对同一份语料算出两个指纹（CI 直接红在语料冻结守卫上）。
  `ledger_digest()` 是去掉 `ENVIRONMENT_DERIVED_KEYS` 后的身份，跨解释器恒定；包含字节码摘要的
  `ledger_fingerprint()` 只作 provenance。
- **一个数字兼两个角色，最后一定会让人拿错那一个**：`core_corpus_fingerprint` 同时被当成“语料身份”和
  “provenance”用了整整一轮，结果 3.11 上必然对不上本机，看起来像回归。身份与指纹要**分开命名、分开发布**：
  `core_corpus_digest` 给两机器对齐，`core_corpus_fingerprint` 留作运行现场。旧名不许“顺手重定义”，
  否则已经发布过的值会变成另一个东西。
- **新增派生字段会静默改掉旧哈希**：把 `corpus_version` / `core_corpus_digest` 加进 payload 时，若不同时把
  它们排除出 canonical 输入，同一份冻结 evidence 的 legacy 与 semantic 哈希就都变了——而它一个字节都没改。
  加字段的同时要在两个 exclusion 集合里登记，并用**已发布的冻结 payload**做 pre/post 对照试验，
  不要只测“新建的结果自洽”。
- **只记 fingerprint、不记 body，等于没有身份**：spec 一旦切版本，旧指纹就变成只能“信”的数字。
  切版本时必须同时归档**旧 body**（`_SPEC_V2_OPERATORS`），并用一条断言把它钅在发布过的常量上；
  归档表要整段冻结，不要用“今天目录减去新 operator”去推导——那会让旧身份随无关元数据改动而静默漂移。
- **“用旧指纹 + 今天的目录”推出来的旧 case 集是假的**：case 的 operator 由 family 轮转决定，
  而轮转读的是目录。只覆盖 seed 的 spec 参数会得到一套“从未跑过”的旧用例集（晋升后它会包含新 operator）。
  正确做法是连目录一起存档（`archived_operator_catalogue()`），`generate_cases(..., catalogue=...)` 同时接受两者。
- **目录里靠推导得出的字段会在第二个特例出现时变成谎言**：`base_variant` 原来靠“有 builder 就是
  three_valves”推，第二个底座（three_valves + 支管）一出现就不对了。现在它是 `MutationOperator`
  上的显式字段，发布在 spec 里。
- **退休一个语料要留下它的身份**：staging 语料被 promote 后就不能再重算它的 digest/hash（代码变了），
  因此把它们**录下来**（`SUPERSEDED_EXTENSION_IDENTITIES`）并标注“记录，不重算”，否则下次有人会拿
  一个算不出来的数字去对账。
- **`exclude` 只作用于顶层**：`canonical_digest(model, exclude=...)` 里的 volatile 字段名只删顶层，
  嵌套同名键全部留下（实测同一 SHA 两次运行有 724 处嵌套 volatile 叶子进入 legacy 哈希）。
  要跨运行可比就自己递归剥（`canonical_repair_result_payload` / `repair_semantic_digest`）。
- **一个没有 contract 的 payload 不该看起来像不匹配**：给已发布的旧 payload 加检查时，必须区分
  “没有这个字段”与“字段不匹配”，否则历史 evidence 会在新验证器下集体变红。
