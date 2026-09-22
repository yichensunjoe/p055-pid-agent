# M6 受治理的语义摄取（Governed Semantic Ingestion）任务书

Charter 参考：§51（原「Existing Drawing Understanding」）、§7（权限边界）、§19（审计与溯源）、
§21.6（可复核性）。本任务书的口径来自远端 Reviewer / Release Gate 2026-09-22 的 M6 十条（`reply26`），
本地不再另立一套架构。

**本文件是 M6 第一阶段的交付物。** 第一阶段只做**设计与契约**：不写摄取运行时、不加 HTTP/MCP 表层、
不落候选持久化、不建真值语料。契约的机器可检查部分在
`backend/agentcad/m6_ingestion_contract.py`，由 `backend/tests/test_m6_ingestion_contract.py`
把本文件的措辞与那份数据绑在一起——两边不一致就红，避免"文档说禁止、代码里已经开了口"。

---

## 0. 成功标准（首页那句话）

> **M6 的成功标准不是"模型能看懂多少图"，而是"未经确认的语义永远不能越过治理边界，而经确认的语义
> 可以确定性、可审计、可撤销地进入工程模型"。**

这句话是整个里程碑的验收口径。任何以"识别率更高"为理由放宽治理边界的方案，在 M6 里都是**错的方向**，
不管它把数字做得多好看。

## 1. 目的与非目标

**目的**：让外部来源（他人交付的图纸、扫描件、旧版 P&ID）上的**语义判断**能够进入本项目的工程模型，
且进入路径可治理。

**非目标（M6 v1 明确不做）**：

- 不做自动接收（auto-accept）的任何类别。第一版不开放 `proposed → applied`，也不开放任何"模型很确定
  所以跳过人工"的捷径。
- 不做删除与拓扑替换。见 §7。
- 不做第二套 writer。所有写入经过既有 apply-v2（§9）。
- 不把"模型输出格式"当成工程变更。candidate 不是 patch（§3）。
- 不追求语料规模。第一版宁可小而精（§12）。

**一个必须先说清的边界**：M6 的输入是**外部工件**，不是本项目自己生成的图。对自产图纸，语义已经在
工程模型里；只有当语义来自图纸外部（别人的交付、扫描件、历史归档）时，才存在"未经确认的判断"这个问题。

## 2. 固定架构：八层与权限边界

流程固定为下面这条链，不再探索另一套方案。每层都有自己的**不可变 id**，因此
"这条提交的设备来自哪份工件、哪个 revision、哪个区域、哪个生产者、哪条证据、谁确认的、经哪个事务写入"
是一路可追的，而不是靠日志。

| # | 层（layer key） | 不可变 id | 由谁产生 | 工程写权限 | 需要人的决定 | 证据义务 |
|---|---|---|---|---|---|---|
| 1 | `imported_artifact` | `artifact_id` | 导入表层（人） | 无 | 否 | `source_document_id`、`source_revision` |
| 2 | `source_region` | `region_id` | 确定性抽取 | 无 | 否 | `artifact_id`、`geometry_selector`、`element_refs`、`text_spans` |
| 3 | `semantic_candidate` | `candidate_id` | 规则引擎 / TypeSafe / repair-LLM | 无 | 否 | `region_id`、producer 与版本、confidence、evidence |
| 4 | `review_decision` | `review_decision_id` | 人工审阅者 | 无 | **是** | `candidate_id`、reviewer action、审阅者身份、时间 |
| 5 | `confirmed_semantic_finding` | `finding_id` | 审阅决定 | 无 | **是** | `review_decision_id`、被确认的事实、provenance 链 |
| 6 | `structured_engineering_patch` | `patch_id` | 确定性 patch compiler | 无 | 否 | `finding_ids`、compiler 版本、编译规则 |
| 7 | `apply_v2_transaction` | `transaction_id` | apply-v2 闸门 | **有（唯一）** | **是** | `patch_id`、dry-run、locality check、conflict check、policy verdict |
| 8 | `committed_revision` | `revision` | 文档存储提交 | 无 | 否 | `transaction_id`、audit record、revision evidence |

```text
导入工件 → 区域 → SemanticCandidate → 人工审阅 → ConfirmedSemanticFinding
        → 确定性编译 → StructuredEngineeringPatch → apply-v2 闸门 → 已提交 revision
```

**一条铁律（可机械检查）**：整条链上**只有 `apply_v2_transaction` 有工程写权限**
（`SOLE_WRITE_LAYER`）。`committed_revision` 记录的是那个事务**产出的结果**，权限属于事务而不属于
被它创建的 revision。契约自检就是盯着这一条：谁想让 candidate 或 patch "顺手写一下"，必须同时在
契约数据里改名，而改名会立刻让测试红。

**为什么 candidate 必须独立建模**：未经人工确认的模型判断**绝不能天然获得 patch 权限**。如果把
candidate 直接表示成 `StructuredEngineeringPatch` 或 `SemanticDiff`，那么"产生一个判断"和"获得一次写入"
在类型上就变成同一件事，治理边界只能靠约定维持。分层让边界变成类型问题。

### 2.1 区域身份（`region_id`）不是「坐标永远不变」

`source_region` 同时存三类 selector（`SOURCE_REGION_SELECTOR_KINDS`），不二选一：`geometry_selector`
（几何区域）、`element_refs`（元素引用）、`text_spans`（文本 span）；另存 frame 字段
（`SOURCE_REGION_FRAME_FIELDS`）：`source_revision`、`page`、`layer`、`coordinate_frame`。
扫描件可以只有几何区域；矢量图可以同时有 element refs；OCR 抽取可以附 text span。

`region_id` 的契约（`REGION_IDENTITY_CONTRACT`）是：

> **在同一个 `source_document_id` + `source_revision` 下，可以确定性重新定位 / 裁出同一证据区域。**

不是「坐标永远不变」。因此 `SOURCE_REVISION_CHANGE_PRODUCES_NEW_REGION_IDENTITY = True`：
source revision 一变，**必须产生新的区域身份**，不能静默沿用旧区域——否则一张已经作废的图上的裁剪
会被当成新图事实的证据。

### 2.2 review queue 的位置：同库、独立聚合、独立生命周期

远端裁定（`REVIEW_PERSISTENCE`）：**同一个 datastore，独立表 / 聚合**，理由是 FK、revision 比较与
事务完整性；**不是为了把 review 状态塞进图纸对象**。两条硬约束：

- `candidate` / `review_decision` / `confirmed_semantic_finding` 是**独立持久化实体**，各有不可变 ID；
- 删除文档或工程元素时**不得**级联删除审阅历史（`cascades_on_document_or_element_delete = False`，
  `shares_lifecycle_with_drawing_objects = False`）。

人工点「确认并应用」时，review decision 与 apply-v2 **可以处在同一个数据库事务里**
（`may_share_a_database_transaction_with_apply = True`，这样崩溃不会留下「确认被吃掉、写入没发生」），
但逻辑上必须仍然留下独立的 `review_decision_id`：apply transaction **只能引用它，不能把它折叠掉**
（`review_decision_id_is_referenced_not_folded = True`）。

### 2.3 conflict 基准与乐观并发控制

「既有权威语义」有一个唯一基准（`CONFLICT_BASELINE`）：

> **apply-v2 管理的、当前已提交的工程语义状态（current committed engineering semantic state）。**

**不是**原始图纸、**不是** candidate、**不是** TypeSafe 的输出（`CONFLICT_BASELINE_IS_NOT` =
`raw_drawing` / `candidate` / `typesafe_output`）。

比较键至少抽象成 **目标工程身份 + 语义路径**（`CONFLICT_COMPARISON_KEY` = `target_engineering_identity` +
`semantic_path`），例如：

```text
equipment:P-201 / equipment_tag
equipment:P-201 / equipment_class
connector:C-17 / endpoints
relationship:T-101->T-102 / existence
```

review 时记录 `baseline_revision` 与 baseline semantic digest；**真正 apply 之前必须重新读取 current
committed revision**（`CONFLICT_RECHECKS_BASELINE_BEFORE_APPLY = True`）。若
`reviewed baseline != current authoritative value`，就进入 `conflicted`——**即使 candidate 本身没有变化**
也不能覆盖（`BASELINE_CHANGE_FORCES_CONFLICTED = True`）。这就是 M6 的乐观并发控制：防止「人确认时是对的，
但应用前工程模型已经被别人改了」。

解除 conflict 必须产生**新的 reviewer decision 或明确的 conflict-resolution decision**
（`CONFLICT_RESOLUTION_REQUIRES_NEW_REVIEW_DECISION = True`），不能复用旧确认偷偷覆盖新状态。

## 3. `SemanticCandidate` 独立 schema

candidate 复用 `SemanticDiff` / `StructuredEngineeringPatch` **只允许出现在后两层**
（`confirmed_semantic_finding → structured_engineering_patch → apply_v2`）。candidate 本身是独立
schema，字段如下（名称与 `SEMANTIC_CANDIDATE_FIELDS` 一一对应，测试会核对）：

| 字段 | 必需 | 含义 |
|---|---|---|
| `candidate_id` | 是 | 这条提案的不可变 id |
| `source_document_id` | 是 | 关于哪份文档 |
| `source_revision` | 是 | 关于该文档的哪个 revision |
| `source_region` | 是 | 判断所依据的区域裁剪 / `element_refs` |
| `candidate_type` | 是 | 这是哪一类事实 |
| `proposed_semantics` | 是 | **被声明的事实**，从来不是写操作 |
| `confidence` | 是 | 对确定性的**描述**，不是权限（§5） |
| `evidence` | 是 | 观察到了什么，好让审阅者能反驳"观察"本身 |
| `producer` | 是 | 规则引擎 / TypeSafe / repair 模型，及其版本 |
| `provenance` | 是 | 模型或 provider 身份、所用过程或规则版本 |
| `conflicts` | 是 | 与既有权威语义的冲突声明（可为空） |
| `review_status` | 是 | 在 §6 状态机中的位置 |
| `supersedes` | 否 | 被它取代的那条 candidate |
| `derived_from` | 否 | 上游 candidate |
| `created_at` | 是 | 提案产生时间 |

`candidate_type` 取值：`symbol_class`、`equipment_tag`、`annotation_role`、`connection_relationship`、
`unresolved`。

**`proposed_semantics` 里写事实，不写操作。** 允许：

```text
symbol_class          = centrifugal_pump
equipment_tag         = P-201
annotation_role       = equipment_label
connection_relationship = T-101 -> T-102
```

不允许（这些属于 patch compiler，写进 candidate 就是越过边界）：
`delete_element`、`update_element`、`move_element`、`write_connector`、`replace_symbol`、
`clear_document`，以及任何其他写操作。

**`unresolved` 是一个诚实的取值，不是一个失败的兜底。** 它表示"这里没有可确认的事实"，
覆盖语料维度里的"无决策"（`ambiguous_no_decision`）与"证据不足"（`insufficient_evidence`）。
`UNCONFIRMABLE_CANDIDATE_TYPES = ("unresolved",)`：它可以被审阅、可以被拒，但因为它不携带任何
可确认的事实，所以它**不能**走到 `confirmed`。"看不出结论"因此是被记录、被审计的结论，
而不是被悄悄丢掉。

## 4. 三个判读来源的职责分离

| producer key | 职责 | 可以产出 | 绝不可以 |
|---|---|---|---|
| `deterministic_rule_engine` | 确定性解析、格式约束、几何关系、既有 schema 校验 | `deterministic_fact`、`candidate` | 授予 apply 权限、扩大自己的白名单、改工程模型 |
| `typesafe` | bounded judgment / classification（**semantic judge**） | `candidate`、`classification`、`confidence`、`evidence` | 授予 apply 权限、生成任意工程写操作、写工程模型、充当 patch compiler |
| `repair_llm_model` | 候选解释、歧义消解、结构化建议 | `candidate`、`proposal` | 授予 apply 权限、绕过人工确认、写工程模型 |

- **TypeSafe 在 M6 是 semantic judge，不是 database writer。** 它输出判断、置信度与依据；
  它不拥有 apply 权限，也不生成任意工程写操作。
- **规则引擎的白名单在 M6 v1 是空的**：`AUTO_ACCEPT_WHITELIST = ()`。
  类别名已经声明（`AUTO_ACCEPT_CLASSES_DECLARED = ("deterministic_fact",)`），但**没有任何一条被开启**，
  所以 v1 里"走到 confirmed"的每一条路径都经过人。要开启某一类，必须单独签一个
  "deterministic auto-accept class" Gate——那是未来的事，不是本阶段的事。
- 三个来源的 `may_grant_apply_authority` 都是 `False`、`requires_human_review` 都是 `True`，
  契约自检会对每一条都做检查。

## 5. confidence 不是 authority

即使 `confidence = 1.00` 也不能直接变成 governed write。字段形状：

```yaml
confidence:
  value: 0.0 … 1.0
  calibration_class: not_measured | measured_on_gold_corpus | inherited_from_producer
  source: model | deterministic_rule | consensus
  meaning: 本次判断的分类置信度；不是正确性保证，也不是权限
```

**唯一决定能否 apply 的是这四项**（`AUTHORITY_DECISION_INPUTS`）：

```text
review_status · policy_verdict · conflict_state · apply_v2_validation
```

confidence **不在**这四项里，而且契约自检会拒绝任何一个"看起来像置信度"的名字混进决定输入。
一个来自不同 producer 的 1.00 和一个 0.62 在法律上都一样：都只是一条待审阅的提案。

`calibration_class` 必须诚实声明。**M6 v1 默认禁止 `measured_on_gold_corpus`**：它是一个由独立
Calibration Gate 签发的身份，不是 producer 可以自己颁的标签；并且**不得自行写一条「达到 N 条就自动获得
calibrated 身份」的规则**（`CALIBRATION_CLASS_AUTO_PROMOTION_RULE = None`）——为了赶 v1 随手造一个
没有统计意义的样本量阈值，比诚实地写 `not_measured` 更差。在 Gate 出现以前，所有 producer 报
`CALIBRATION_DEFAULT_CLASS = "not_measured"`。

Calibration Gate（由**远端 Release Gate** 签署，`CALIBRATION_GATE_SIGNER`）至少必须签
（`CALIBRATION_GATE_REQUIRED_EVIDENCE`）：

```text
immutable_gold_corpus_id_and_version · candidate_type
train_and_research_material_do_not_overlap_held_out_gold
sample_count_and_label_outcome_distribution · calibration_metric_and_computation_version
coverage_and_abstention_conventions · producer_and_model_version · calibration_report_hash
```

## 6. review queue 是显式状态机

状态（`CANDIDATE_STATES`）：

```text
proposed · needs_review · confirmed · rejected · superseded · conflicted · applied
```

允许的边（`from --trigger--> to`，方括号是必须留下的证据）：

```text
proposed     --producer_files_candidate-->        needs_review
proposed     --contradicts_existing_authoritative_value--> conflicted   [conflict_record]
proposed     --replaced_before_review-->          superseded            [successor_candidate_id]
needs_review --human_confirm-->                   confirmed             [reviewer_action, review_decision]
needs_review --human_reject-->                    rejected              [reviewer_action, review_decision]
needs_review --conflict_detected-->               conflicted            [conflict_record]
needs_review --replaced_during_review-->          superseded            [successor_candidate_id]
conflicted   --human_resolves_conflict-->         needs_review          [conflict_resolution, new_review_decision, reviewer_action]
conflicted   --human_rejects_conflict-->          rejected              [reviewer_action]
conflicted   --replaced_while_conflicted-->       superseded            [successor_candidate_id]
confirmed    --authoritative_baseline_changed_before_apply--> conflicted [baseline_recheck, conflict_record]
confirmed    --apply_v2_accepted-->               applied               [compiled_patch, policy_verdict, baseline_recheck, conflict_check, apply_v2_validation]
confirmed    --replaced_after_confirmation-->     superseded            [successor_candidate_id]
rejected     --replaced_after_rejection-->        superseded            [successor_candidate_id]
applied      --replaced_after_apply-->            superseded            [successor_candidate_id]
```

**两条性质是自检出来的，不是表格里的一句话**：

1. **`applied` 只有一条入边，来自 `confirmed`。** 于是不可能存在任何绕过确认的"应用"路径；
   契约自检遍历声明的边来验证这一点，新增一条边就会红。
2. **whitelist 为空时，进入 `confirmed` 的每一条边都必须记录 reviewer action 与 review decision。**
   人工确认必须**记录审阅动作**，而不是只把状态字段改成 `confirmed`。
3. **乐观并发是机器不变量**（§2.3）：入 `applied` 的边必须带
   `BASELINE_RECHECK_EVIDENCE`（`baseline_recheck`），且必须存在一条
   `confirmed --authoritative_baseline_changed_before_apply--> conflicted` 的边；
   离开 `conflicted` **重新走上确认路径**（去 `needs_review` / `confirmed` / `applied`）必须带
   `new_review_decision`（直接 `superseded` 不算重新进入确认路径，有意豁免）。
   合起来就是：

   ```text
   confirmed finding + baseline changed before apply → conflicted → 禁止进入 applied
   ```

   即使确实走出了 conflict，也必须经过一次**新的**确认（旧确认不能当新基准的通行证）。

明确禁止（`FORBIDDEN_TRANSITIONS`，写出来是为了让"禁止"可被复查，也让误加回来的边立刻失败）：

```text
proposed → applied        （治理边界的第一条禁线）
proposed → confirmed      （无审阅记录的确认）
needs_review → applied
conflicted → applied
rejected → applied
superseded → applied
applied → confirmed
applied → rejected
```

`superseded` 是一个诚实的终态：旧判断被新判断取代时，它不会被删除，也不会被改写——
`supersedes` / `derived_from` 指过去，历史仍然读得懂。

## 7. v1 写入策略：破坏性意图默认禁止

| 意图 | v1 处置 | 理由 |
|---|---|---|
| `creation` | `allowed_after_confirmation` | 新增对象不与任何既有权威声明冲突 |
| `metadata_enrichment` | `allowed_after_confirmation` | 补一个没人说过的属性是加法；改一个已经说过的值是覆盖，不算 enrichment |
| `relationship_addition` | `allowed_after_confirmation` | 新连接可以在落地前被 dry-run 与 locality check 验证 |
| `overwrite_existing_authoritative_value` | `conflict_requires_human_resolution` | "模型认为旧内容错了"是一个假设，不是一次更正；它首先产生 **conflict candidate** |
| `delete_existing_engineering_object` | `forbidden` | 删除对"增加理解"没有必要性，而它的爆炸半径是整个拓扑 |
| `replace_topology` | `forbidden` | 替换连通性是后果最大的写操作，M6 没有不是"重写"的用例 |

**这是一条有意的默认拒绝（default deny）**：M6 的第一版只让它**更懂图**，不让它**改图**。
把 `overwrite` 划成 conflict 而不是 write，是这套设计里最容易被"顺手优化掉"的一条，所以它进了契约数据。

## 8. 人工确认的是事实，不是裸 patch

审阅者确认的是：

```text
这个符号确实是离心泵 P-201。
```

而不是"把第 3 个元素改成 `centrifugal_pump`"。事实被确认后才由**确定性 compiler**生成 patch：

```text
ConfirmedSemanticFinding → StructuredEngineeringPatch
```

这样做的直接后果：**将来 patch 格式变化，不会让历史人工决策失效。**
一份 2026 年确认的"这是 P-201"在 patch 编译器改版后依然是同样的意思，重放它不需要重放一个已废弃的
patch 格式。

## 9. apply-v2 是唯一生产写入口

M6 不新造第二套 writer。确认后的候选必须走完整条：

```text
candidate → confirmed finding → patch compiler → dry-run / validation
          → locality check → conflict check → apply-v2 → evidence
```

沿用既有 governed write、undo/redo、protected state、删除策略；沿用"一个 revision + diff + audit +
harness close-out 在同一个 SQLite 事务里提交"的原子性（见 `docs/audit-and-provenance.md`）。
表层登记仍然受 `backend/agentcad/surface_contract.py` 约束：**没有未登记的写路径**这条测试对新表层
同样有效（本阶段不新增表层，见 §14）。

## 10. provenance 必须能回答四个问题

任何最终写入都必须可追溯：

1. **从哪张图、哪个 revision、哪个区域来的** —— `artifact_id`、`source_revision`、`region_id`；
2. **哪个 producer / model / rule 得出的** —— `producer`、`provenance`；
3. **哪个 evidence 支撑** —— `evidence[]`；
4. **谁确认的、通过哪个 transaction 写进去的** —— `review_decision_id`、`finding_id`、
   `patch_id`、`transaction_id`。

链（每层一个不可变 id）：

```text
source artifact → extraction → candidate → review decision → confirmed finding
                → compiled patch → apply-v2 transaction
```

`supersedes` / `derived_from` 只是**新增**一条 candidate，从不改写上面任何一层。

## 11. undo 与 replay 是两件事

| | 含义 | 做法 |
|---|---|---|
| **undo** | 撤销**已经应用的工程写入** | `UNDO_MECHANISM = compensating_governed_transaction_through_apply_v2`：一次新的、反向的、同样受治的写事务 |
| **replay** | 用**当时被冻结的输入**再跑一次，验证结果一致 | 冻结 `candidate` + `review_decision` + `patch_compiler_version` + `patch_compiler_rules` + `authoritative_baseline_revision`，验两层 digest（见下） |

### 11.1 replay 验的是语义，不是字节（远端裁定的修改）

初稿把 replay 写成「已应用事务的操作**逐字节**比较」（`REPLAY_MUST_BE_BIT_IDENTICAL`）。
远端否决了这一点，并要求改掉——理由与 A5 那次的错是同一个：**把运行时 metadata 混进身份**。
`transaction_id`、timestamp、审计时间都是合理的 volatile provenance，它们不一致不应让 replay 失败。
现在改成两层（都要求一致）：

```text
REPLAY_CANONICAL_PATCH_MUST_MATCH   = True   # 规范化后重新编译的 patch digest
REPLAY_SEMANTIC_POSTSTATE_MUST_MATCH = True  # 应用在相同 baseline 后的语义状态 digest
REPLAY_RAW_TRANSACTION_BYTES_MUST_MATCH = False   # 明确不是契约
REPLAY_VOLATILE_FIELDS_EXCLUDED = transaction_id · timestamps · audit_time
```

冻结输入（`REPLAY_FROZEN_INPUTS`）：`candidate` 、`review_decision`、`patch_compiler_version`、
`patch_compiler_rules`、`authoritative_baseline_revision`。

### 11.2 undo 不是状态回滚，但事务层要能表达它

- 已确认的事实保持 `confirmed`——事实不会「没发生过」；撤销是一次独立的、可审计的**补偿事务**
  （`TRANSACTION_REVERTED_MEANS_COMPENSATED_NOT_ERASED = True`），不篡改历史 review decision。
- **不新增 candidate 级 `reverted` 状态**（`CANDIDATE_LEVEL_REVERTED_STATE = False`）。
  但 **transaction 层必须能表达** `TRANSACTION_STATES`：

  ```text
  applied · reverted · superseded
  ```

  审阅界面若要显示「这个确认曾应用、后来被撤销」，用**派生展示态**
  （`REVERTED_IS_A_DERIVED_PRESENTATION_STATE = True`）：

  ```text
  confirmed + last applied transaction reverted
  ```

  不把它变成 candidate 状态机里的新权威状态。

**M6 Release Gate 应要求关键 case 可 replay，而不只是能 undo。**

## 12. gold corpus 从零建立

**R8 那 112 条标注 × 2 个判断不是 expected truth。** 它们只能进入：

```text
research_examples/            （SEED_MATERIAL_PATHS）
candidate_seed_material/
```

这两个目录**不是语料路径**（`GOLD_CORPUS_PATH = backend/tests/m6_gold_corpus/`），
`SEED_MATERIAL_COUNTS_AS_EXPECTED_TRUTH = False`，且契约自检断言语料路径不在种子材料之下。

正式语料每一条都必须经过**独立人工标注**，至少保存（`GOLD_ITEM_REQUIRED_FIELDS`）：

```text
source_crop_reference · expected_semantic_fact · acceptable_ambiguity
forbidden_interpretation · review_provenance
```

第一版不要追求 112 条。宁可做一组**小而高质量**的 gold corpus，覆盖七个维度
（`GOLD_CORPUS_DIMENSIONS`）：

```text
symbol_classification · tag_association · annotation_role · connection_relationship
· ambiguous_no_decision · conflict_with_existing_semantics · insufficient_evidence
```

`acceptable_ambiguity` 与 `forbidden_interpretation` 是这个语料与普通分类语料最大的区别：
P&ID 上"BALL VALVE"该被读成设备标签还是注释文字，往往两种都讲得通——语料要记录**哪种可接受**、
**哪种不可接受**，否则会把"合理的另一种读法"当成错误来训练。

## 13. 契约的机器可检查部分

`backend/agentcad/m6_ingestion_contract.py` 把上面的决定写成可被反驳的数据，
`contract_document()` 可以整份导出（供证据引用，而不是转述）。`validate_contract()` 检查的正是
最容易在实现阶段被悄悄放宽的那几条：

1. 八个层的 position 唯一且递增；每层贡献不同的不可变 id；
2. **有工程写权限的层恰好是 `apply_v2_transaction` 一个**；
3. 三个来源都没有 apply 权限、都必须人工审阅、都没有开启自动接收；
4. 授权决定输入里没有"像置信度"的名字；
5. 状态机：边不指向未声明状态；声明的禁止边确实不在机器里；**`applied` 唯一入边来自 `confirmed`**；
   入 `applied` 的边必须带齐闸门证据**且必须带一次 fresh baseline recheck**；whitelist 为空时
   入 `confirmed` 的边必须带 reviewer action；**基线变动必须能把 `confirmed` 打回 `conflicted`**，
   而离开 `conflicted` 重新进入确认路径（`needs_review` / `confirmed` / `applied`）必须带
   `new_review_decision`；
6. candidate schema 含必需字段、**不含** patch 操作字段；
7. 破坏性意图必须是 `forbidden`，每行必须写明理由；
8. 种子材料不算真值、语料路径不在种子材料下；
9. **区域身份**：三类 selector 都必存；source revision 变化必须产生新区域身份；
10. **review 持久化**：不与图纸对象共生命周期、删除不级联、`review_decision_id` 只能被引用不能被折叠；
11. **conflict 基准**：必须是 apply-v2 管理的已提交语义状态，原始图纸 / candidate / TypeSafe 输出
    都不得当权威；比较键含身份 + 语义路径；apply 前必须重读 baseline；
12. **calibration**：`measured_on_gold_corpus` 必须由 Gate 签，不得存在自动晋升规则，默认类不得是受闸类；
13. **事务层状态**：`applied` / `reverted` / `superseded` 齐备，`reverted` 语义是补偿而非抹除，
    且不得出现在 candidate 状态里；
14. **replay**：canonical patch 与语义后态两者都必须一致，**原始事务字节不是契约**，volatile 字段必须排除；
    undo 必须走同一个受治写路径；
15. **持久身份格式**：每条内容派生身份的前缀与 digest 宽度都由 `PERSISTENT_IDENTITIES` 声明，
    前缀两两不同，且**任何身份都不得被截断**（`IDENTITY_FULL_DIGEST_HEX = 64`）。
    核心代码通过 `identity_prefix(field)` 取用，不再在实现里写字面前缀；
    测试再从**本文件**把所有 ``前缀<宽度>`` 写法抽出来，与声明逐条对拍——于是“文档说 16 hex、代码写 64 hex”
    这种同文不同节的自相矛盾会被测试直接判红，而不是等 Gate 肉眼核对。

`backend/tests/test_m6_ingestion_contract.py` 在此之上再钉两件事：
**本文件的措辞与契约数据一致**（层名、状态名、策略意图、语料维度、candidate 字段逐个核对），
以及**第一阶段没有偷偷新增表层**（枚举活的 OpenAPI 与 MCP 工具名，禁止出现候选/摄取类路由）。

写这份契约时自检立刻抓到一个真错误：我一开始给 `committed_revision` 也标了工程写权限——
它记录的是事务产出的结果，权限属于事务而不属于被创建的 revision。这正是"把权限写成数据"的用处。

## 14. 阶段边界

**第一阶段（本轮）交付**（`PHASE_1_DELIVERABLES`）：

```text
task_book · governance_contract_data · doc_contract_agreement_test
```

**第一阶段禁止**（`PHASE_1_FORBIDDEN`）：

```text
ingestion_runtime · candidate_persistence · new_http_route · new_mcp_tool
review_queue_api · patch_compiler · gold_corpus_claim
```

对应的表层令牌（`PHASE_1_FORBIDDEN_SURFACE_TOKENS`，测试对着活的表层枚举）：

```text
candidate · ingestion · ingest · semantic-finding · confirmed-finding · review-queue
```

也就是说：**本阶段回答"边界在哪"，不回答"怎么实现"**。后续阶段的顺序建议（等 Gate 签署后）：

1. semantic candidate schema 与 review state machine 的实体化 + review queue 表层（此时才登记表层）；
2. 规则引擎的确定性抽取 + TypeSafe 的 bounded judgment 接入（两者都只产出 candidate）；
3. 事实 → patch 的确定性 compiler + replay harness；
4. gold corpus 第一批（七个维度各若干条）+ 基于它的 calibration measurement。

## 15. 六项裁定（2026-09-22 远端 Gate 正式答复，已并入上面各节）

初稿把六个问题留成「未决」，远端裁定如下（**这才是最终口径**，前面各节已按此改写）：

1. **`source_region`**：两类引用都存，不能二选一；支持 `geometry_selector` / `element_refs` / `text_spans`
   三类 selector + frame 字段。`region_id` 的契约是「同一 source revision 下可确定性重定位 / 裁出同一证据
   区域」，不是「坐标永远不变」；revision 变则**必须**产生新区域身份。→ §2.1
2. **review queue**：**同一 datastore，独立表 / 聚合**；三个实体各有不可变 ID；
   **文档或元素删除不得级联删除审阅历史**；「确认并应用」可在同一个数据库事务里，但
   `review_decision_id` 只能被引用、不能被折叠。→ §2.2
3. **conflict 基准**：以 **apply-v2 管理的 current committed engineering semantic state** 为唯一权威基准，
   不用原始图纸 / candidate / TypeSafe 输出；比较键 = 目标工程身份 + 语义路径；review 时记录 baseline，
   **apply 前必须重读**；`reviewed baseline != current authoritative value` 即 `conflicted`。
   即为乐观并发控制，并配套机器不变量（见 §6 性质 3）。→ §2.3
4. **`measured_on_gold_corpus`**：**M6 v1 默认禁止**，必须独立 Calibration Gate（仍由远端 Release Gate 签），
   并至少附八项证据；**不得写「达到 N 条即自动 calibrated」的规则**。→ §5
5. **replay**：**改掉「原始 transaction bytes 全字节相同」**（这是远端唯一明确要求改掉的既有选择），
   改为两层：canonical compiled patch digest 与 resulting semantic state digest 都必须一致，
   明确排除 transaction id / timestamps 等 volatile provenance。→ §11.1
6. **candidate 级 `reverted` 状态**：不增加（本地原选择被批准）。但 **transaction 层**必须能表达
   `applied` / `reverted` / `superseded`；界面需要时用派生展示态
   `confirmed + last applied transaction reverted`，不得变成新的权威 candidate 状态。→ §11.2

**目前本任务书没有遗留未决项**；若实现阶段出现新的口径分歧，同样先写进本文件再报 Gate。

## 16. 实现落点（随阶段推进填写）

| 关注点 | 文件 | 状态 |
|---|---|---|
| 任务书（本文件） | `docs/m6-governed-semantic-ingestion.md` | 第一阶段 ✓ |
| 治理契约（层 / 状态机 / 策略 / 来源 / 区域身份 / 并发基线 / 校准闸 / replay / 语料维度） | `backend/agentcad/m6_ingestion_contract.py` | 第一阶段 ✓ |
| 文档 ↔ 契约 一致性与表层边界 | `backend/tests/test_m6_ingestion_contract.py` | 第一阶段 ✓ |
| candidate schema 实体 | `backend/agentcad/m6_candidate_models.py` | **Phase-2A ✓** |
| 治理核心（状态机执行 / 基线重读 / 确定性编译） | `backend/agentcad/m6_candidate_core.py` | **Phase-2A ✓** |
| review queue 与状态机持久化 | `backend/agentcad/database_recovery.py`（schema v7）+ `store.py` | **Phase-2A ✓** |
| 事实 → patch 的确定性 compiler | `m6_candidate_core.py`（仅 `creation` / `metadata_enrichment`） | **Phase-2A 子集 ✓** |
| replay harness | 待定 | 待 Gate 签署 |
| gold corpus | `backend/tests/m6_gold_corpus/` | 待 Gate 签署 |

## 16b. Phase-2A 已落地（内部 domain/service 层，无表层）

已实现的最小闭环：

```text
SourceArtifactRef → SourceRegion → SemanticCandidate → ReviewDecision
→ ConfirmedSemanticFinding → 确定性编译 → StructuredEngineeringPatch
```

四个设计点比“有哪些文件”重要得多：

1. **状态是算出来的，不是存出来的。** `semantic_candidates` 只存 `review_status_at_creation`；
   当前状态由 **append-only 的 `review_decisions` 日志回放**得到，而回放的每一步都必须命中契约里
   声明的边。于是“把 `confirmed` 写进一列”不给任何权限——“确认”只能是一行人类决定。
2. **`patch_id` 由 digest 派生**（`m6patch_<64 hex>`）。同一条 finding 编译两次得到**同一个身份与同一份内容**，
   不存在“看起来差不多”的两张 patch；而 digest 的输入排除了 `created_at` / `decided_at` / `transaction_id`
   等 volatile 字段（§11.1 的同一个教训）。
3. **persistence 是同一库上的三个独立聚合**，外键布局由两个不同的问题分别回答（混用一个规则会把其中一个答错）：

   - *证据是否活得更久？* 所以指向 `documents` 的外键**故意不存在**（沿用 `audit_records` 的先例：证据必须活得比
     它作证的东西久；`CASCADE` 会跟着图纸删掉审阅历史，而 `RESTRICT` 又会让图纸删不掉）。
   - *证据是否允许自相矛盾？* 不允许。**M6 内部的链是真外键**，而且必须存在：

     ```text
     review_decisions.candidate_id             → semantic_candidates(candidate_id)              ON DELETE RESTRICT
     confirmed_semantic_findings.candidate_id  → semantic_candidates(candidate_id)              ON DELETE RESTRICT
     confirmed_semantic_findings.(candidate_id, review_decision_id)
                                               → review_decisions(candidate_id, review_decision_id)  ON DELETE RESTRICT
     ```

     最后一条是 **composite FK**（配合 `UNIQUE (candidate_id, review_decision_id)`）：只引用
     `review_decision_id` 的话，“candidate A 的 finding 引用 candidate B 的 decision”仍然可表示；
     两者都引用才使其无法表示。

   三个表都只有 insert 方法：**没有更新路径，是“不可变”的强制手段**。

3b. **一次人工确认是不可分割的两行。** `ReviewDecision(kind=human_confirm)` 与
   `ConfirmedSemanticFinding` 在**同一个数据库事务**里提交（`store.record_confirmation`）。
   分开写会留下一个窗口：状态机已经说 `confirmed`，却没有任何 finding 能追溯到人——这正是 Phase-2B
   获得写权限之前不能继承的裂缝。故障注入测试真的让第二行写失败（SQLite trigger）并断言两行都不存在。
3c. **内容派生的身份取完整 digest，且格式只声明一次。** 五条持久身份的写法如下，出自
   `PERSISTENT_IDENTITIES`（不是本节措辞），并且**核心代码从同一份声明取前缀**：

   ```text
   patch_id             = m6patch_<64 hex>
   review_decision_id   = m6dec_<64 hex>
   conflict_id          = m6cfl_<64 hex>
   finding_id           = m6find_<64 hex>
   generated_element_id = el_m6<64 hex>
   ```

   最后一条的前缀是 `el_m6` —— **没有下划线**（它与其他四条不是同一命名族），而它恰恰是最不能截断的一条：
   编译生成的新元素 id 会成为真正工程对象的身份，在那里一次哈希截断是图纸里的碰撞，不是报告里的碰撞。
   本节此前把两条身份的宽度写成不同值（一处 `digest[:16]`、一处 `<sha256>`）——同文自相矛盾正是
   “格式写在多处”的必然产物，所以现在由契约声明、由代码取用、由测试对拍本文件。
3d. **决策身份是 `review-decision-v1`，由决策的不可变内容派生**，而不是“谁在什么时候把哪一行插进去”：

   ```text
   review-decision-v1 + SHA-256 over canonical payload:
     candidate_id · kind · from_status · to_status
     reviewer_identity · reviewer_action · note
     baseline_revision · comparison_identity · comparison_path
     baseline digest_version · baseline digest · value_present
     conflict_resolution · resolution_choice · successor_candidate_id
   （decided_at 明确排除：记账时间不得能改变身份）
   ```

   只哈希 `(candidate, from, to, kind)` 会把**两个不同的人工决策塌成同一个 id**：同一位工程师分别基于
   revision 7 与 revision 8、理由不同地确认同一件事，会变成一个审计事件——而 `(candidate_id,
   review_decision_id)` 正是 finding 的外键基础，那等于把数据库完整性建在一个分不清两件事的身份上。
3e. **语义值的规范化是 path-aware 的，不是全局的。** 规则属于语义路径：

   ```text
   equipment_tag    → trim + 折叠空白 + casefold（本仓库按 tag 寻址本来就 casefold）
   equipment_class  → 同上（枚举键）
   annotation_role  → exact（不擅自 casefold）
   existence        → exact
   未登记的 path     → hard fail，绝不回退到 tag 规范化
   ```

   全局 casefold 会让“真的改了”读成“没变”，而那正是乐观并发不能有的失效模式。新增一个语义路径因此意味着
   **必须选一个 canonicalizer**（这是应该被看见的决定）。摘要版本不匹配时 `recheck_baseline` **明确报错**
   （`baseline_digest_version_mismatch`），不静默比较。
4. **编译器的能力边界写成了 refusal 而不是猜测**：Phase-2A 只编译 `creation` 与 `metadata_enrichment`；
   `overwrite` → `conflict_requires_human_resolution`（拒绝，不是覆盖）；`delete` / `replace_topology` → 禁；
   `relationship_addition` 与“把已有对象的类别改掉” → 明确拒绝并给出 reason code。

本阶段**未做**（按 Gate 边界）：apply-v2 写入、undo / 补偿事务、HTTP 路由、MCP 工具、UI / review 页、
TypeSafe 与 LLM 的摄取接入、R8 的 112×2 数据、auto-accept、批量导入、replay harness、gold corpus。

## 17. 与已有系统的关系

- **M5**：M5 是"针对一个已知 finding 的受治理修复"，M6 是"从一个未知工件里得出 finding"。
  两者共用 apply-v2 与 audit/evidence，但不共用入口：M5 的 finding 由 validator 产生，
  M6 的 finding 由**人确认 candidate** 产生。
- **M2 工程语义图**：M6 的 `confirmed_semantic_finding` 最终影响的是同一套工程对象与属性；
  "与既有权威语义冲突"的比较基准就是那张图（细则见 §15-3）。
- **TypeSafe**：已经在画图路径上以"代码列候选、模型只回答指的是哪一个"的方式接进来
  （见 `docs/typesafe-drawing.md`）。M6 里它仍是同一个角色——semantic judge——只是候选来源从
  "用户指令"换成"外部工件的观察"。
- **A6 real-model qualification**：处于 `BLOCKED — EXTERNAL PROVIDER DEPENDENCY`，与 M6 无关；
  M6 不因为要拿到一个可跑的模型而改这条状态。
