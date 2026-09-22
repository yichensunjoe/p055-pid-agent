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
| 2 | `source_region` | `region_id` | 确定性抽取 | 无 | 否 | `artifact_id`、几何范围、文本 span |
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

`calibration_class` 必须诚实声明。在 gold corpus 建立之前，TypeSafe 的判断只能是
`not_measured`；用 `measured_on_gold_corpus` 必须有 §12 那条语料与对应的测量证据。

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
conflicted   --human_resolves_conflict-->         needs_review          [conflict_resolution, reviewer_action]
conflicted   --human_rejects_conflict-->          rejected              [reviewer_action]
conflicted   --replaced_while_conflicted-->       superseded            [successor_candidate_id]
confirmed    --apply_v2_accepted-->               applied               [compiled_patch, policy_verdict, conflict_check, apply_v2_validation]
confirmed    --replaced_after_confirmation-->     superseded            [successor_candidate_id]
rejected     --replaced_after_rejection-->        superseded            [successor_candidate_id]
applied      --replaced_after_apply-->            superseded            [successor_candidate_id]
```

**两条性质是自检出来的，不是表格里的一句话**：

1. **`applied` 只有一条入边，来自 `confirmed`。** 于是不可能存在任何绕过确认的"应用"路径；
   契约自检遍历声明的边来验证这一点，新增一条边就会红。
2. **whitelist 为空时，进入 `confirmed` 的每一条边都必须记录 reviewer action 与 review decision。**
   人工确认必须**记录审阅动作**，而不是只把状态字段改成 `confirmed`。

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
| **undo** | 撤销**已经应用的工程写入** | `UNDO_MECHANISM = reverse_governed_transaction_through_apply_v2`：一次新的、反向的、同样受治的写事务 |
| **replay** | 用**当时被冻结的输入**再跑一次，验证结果一致 | 冻结 `candidate` + `review_decision` + `patch_compiler_version` + `patch_compiler_rules`，与**已应用事务的操作**逐字节比较 |

两条硬约束：

- **undo 不是状态回滚**（`UNDO_IS_A_STATE_ROLLBACK = False`）。已确认的事实保持 `confirmed`——
  事实不会"没发生过"；被撤销的是那次工程写入，而撤销本身是一次独立的、可审计的写。
- **replay 必须要求结果一致**（`REPLAY_MUST_BE_BIT_IDENTICAL = True`）。不一致的 replay 说明
  "当时那条决定"不足以复现结果，这正是要发现的东西，不能被当成"环境差异"放过。

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
   入 `applied` 的边必须带齐四项闸门证据；whitelist 为空时入 `confirmed` 的边必须带 reviewer action；
6. candidate schema 含必需字段、**不含** patch 操作字段；
7. 破坏性意图必须是 `forbidden`，每行必须写明理由；
8. 种子材料不算真值、语料路径不在种子材料下；
9. replay 必须要求一致、undo 必须走同一个受治写路径。

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

## 15. 未决问题（明确留给 Gate 裁定）

这些问题**不由本地自行决定**，写在这里是为了让 Gate 一次把口径定完：

1. **`source_region` 的形式**：是几何裁剪框 + 文本 span，还是元素引用集合？扫描件与矢量图的能力不同。
2. **review queue 的持久化位置**：与文档同库（沿用同一个原子提交路径），还是独立的 review store？
   前者让"确认"与"写入"更容易共事务，后者让审阅独立于图纸生命周期。
3. **conflict 的判定基准**：与"既有权威语义"比较时，"权威"指的是工程语义图里的哪个层
   （声明的 `engineering_id` / tag / 属性）？
4. **`calibration_class` 何时允许写 `measured_on_gold_corpus`**：语料规模与测量口径由谁来签。
5. **replay 的比较粒度**：逐字节比较已应用事务的操作（本任务书的默认），还是逐字段语义比较？
   前者更严格，后者对合法的重新编号更宽容。
6. **是否需要独立的 `reverted` 状态**：本任务书选择不加（见 §11），撤销记录在事务层。若 Gate 认为
   审阅者视角必须看到"已被撤销"，再加。

## 16. 实现落点（随阶段推进填写）

| 关注点 | 文件 | 状态 |
|---|---|---|
| 任务书（本文件） | `docs/m6-governed-semantic-ingestion.md` | 第一阶段 ✓ |
| 治理契约（层/状态机/策略/来源/语料维度） | `backend/agentcad/m6_ingestion_contract.py` | 第一阶段 ✓ |
| 文档 ↔ 契约 一致性与表层边界 | `backend/tests/test_m6_ingestion_contract.py` | 第一阶段 ✓ |
| candidate schema 实体 | `backend/agentcad/m6_candidate_models.py`（拟定） | 待 Gate 签署 |
| review queue 与状态机持久化 | 待定（§15-2） | 待 Gate 签署 |
| 事实 → patch 的确定性 compiler | 待定 | 待 Gate 签署 |
| replay harness | 待定 | 待 Gate 签署 |
| gold corpus | `backend/tests/m6_gold_corpus/` | 待 Gate 签署 |

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
