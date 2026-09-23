# M7 — Semantic-First Natural-Language P&ID Synthesis

> 自然语言驱动的大规模 P&ID 语义优先绘制
>
> 契约数据：`backend/agentcad/m7_synthesis_contract.py`（`M7_CONTRACT_VERSION = "m7-synthesis-contract/1"`）
> 本阶段：**Phase-1 — Diagnosable & Expressible Synthesis**（只做设计与契约，不写运行时）

---

## 1. 这个里程碑为什么存在

它不是从"模型画得不好"出发的，是从一次**可复算的失败**出发的。

一个 agent 被要求画一张厂区级气路系统总图，它提交了 **197 个语义操作，其中只有 120 个编译通过**，
剩下 77 个被逐个丢弃。然后**每一个能被看见的地方都在报告成功**：assessment 是 `valid`，
自动执行器的循环条件是 `not valid`，人工确认框写着"计划已通过校验"，而这次会话被记录成
`completed`。一份少了自身计划 39% 的产出，被认定为完成。

同一份数据还暴露了第二件事：**模型在干 CAD 排版器的活**。按字节统计，它必须吐出的 payload 里
**80% 是坐标与样式**，只有 20% 是语义；而仓库里那个确定性排版引擎是被
`preserve_positions=True` 调用的——明确告诉它不要摆放任何东西。

所以 M7 的成功判据不是"多画一点"，而是两句可以被测试反驳的话：

1. **不完整的合成必须自己说出来，并说出缺了哪些部分。**
2. **模型拥有意义，代码拥有几何。**

### 1.1 被证据否定的根因

下面三条**不是**本次事故的根因，写在这里是因为错误的诊断直接产出了那份"改提示词"的建议清单：

- `prompt_quality`
- `model_capability`
- `cad_element_count`

换更强的模型可能改善结果，但不会解决结构问题。

### 1.2 缺陷登记册 `DEFECT_REGISTER`

| 严重度 | 代号 | 含义 |
|---|---|---|
| **P0** | `silent_partial_compilation` | 部分编译的计划被判定为 valid，并记录为已完成会话 |
| **P0** | `model_owns_low_level_geometry` | 一次性计划使模型对大规模图纸里的每一个坐标负责 |
| **P1** | `layout_intent_not_expressible` | 没有任何操作能表达图幅要求，所以"画成横向长图"这个要求无法提出 |
| **P1** | `catalogue_makes_requested_content_unrepresentable` | 可见性屏蔽与真实的目录缺口静默移除必需内容 |
| **P1** | `layout_engine_does_not_own_macro_placement` | 确定性排版引擎以 preserve_positions 被调用，因此它从不摆放 |

---

## 2. 两个轴：`validity` × `completeness`

这是本里程碑唯一最重要的结构决定。

```
validity:      valid | invalid
completeness:  complete | partial | empty
```

把二者塌缩成一个 `valid` 布尔量，正是"39% 丢失却报成功"的机制。
因为这两问是不同的问题、有不同的恢复方式：

- "被接受的 120 条是否各自合法" —— 这是 `validity`
- "计划是否完整" —— 这是 `completeness`

因此 `COMPLETENESS_IS_INDEPENDENT_OF_VALIDITY = True`，且
`COMPLETENESS_IS_DERIVED_FROM_COUNTS = True`（它必须从计数推出，不能由调用方另行声明）。

会话只有在 **`SESSION_SUCCESS_REQUIRES = ("valid", "complete")`** 时才允许成功。

assessment 至少携带 `SYNTHESIS_COUNT_FIELDS`：

```
proposed_operation_count
compiled_operation_count
rejected_operation_count
```

`proposed_operation_count` 是先前缺的那一个——没有它，"120 是从多少里来的"无法回答，丢失不可见。

---

## 3. 提案证据与拒绝回执

**`RAW_PROPOSAL_IS_DURABLE_EVIDENCE = True`。** 被提交的那份计划本身是证据，
已应用的事务**不能**代替它——因为二者之差**就是**那个发现。因此
`APPLIED_ONLY_PERSISTENCE_IS_FORBIDDEN = True`。
证据必须 `EVIDENCE_READABLE_WITHOUT_PROVIDER = True`，即不需要对着一个活的 provider 重放会话。

`PROPOSAL_EVIDENCE_REQUIRED_FIELDS`：

```
plan_id · session_id · document_id · base_revision
proposed_operations · rejected_operations
proposed_operation_count · compiled_operation_count · rejected_operation_count
```

每个被拒操作携带 `REJECTED_OPERATION_RECEIPT_FIELDS`：

```
original_index · operation_id · operation_kind · reason_code
message · field_path · available_values · suggestions
```

编译期**本来就已经算出** `reason_code` / `field_path` / `available_values` / `suggestions`，
然后整包丢掉。把它回传，才是把死路变成重规划。

---

## 4. 自动执行器的延续矩阵 `CONTINUATION_MATRIX`

矩阵对 `validity × completeness` 的叉积**是完备的**，一格里恰好一条规则，
这样未来的第四种情况不能靠沉默被发明出来。

| validity | completeness | action |
|---|---|---|
| `valid` | `complete` | `proceed_to_human_confirmation` |
| `valid` | `partial` | `return_receipt_and_replan` |
| `valid` | `empty` | `replan_or_abort` |
| `invalid` | `partial` | `existing_error_recovery` |
| `invalid` | `empty` | `existing_error_recovery` |
| `invalid` | `complete` | `existing_error_recovery`（构造上不可达，声明出来是为了让矩阵总量化） |

三条禁令：

```
PARTIAL_MAY_BE_REPORTED_AS_SUCCESS = False
PARTIAL_MAY_BE_RECORDED_AS_COMPLETED_SESSION = False
RECEIPT_IS_OMITTABLE = False            ← 调用方不得为了拿到"干净"结果而抑制回执
```

`valid + partial` 必须**既不被当作成功，也不被当作硬错误**——它回传回执、由 agent 修复。
这正是本次事故的位置。

人工授权之前，`UI_MUST_DISPLAY_COMPLETENESS_FIELDS` 必须可见：

```
proposed_operation_count · compiled_operation_count · rejected_operation_count · completeness
```

---

## 5. 目录审计：`hidden` 与 `missing` 是两种缺陷

不能把 PT/PDT/TT/FT 笼统叫作"被隐藏"。审计是一条**链**，每一步都要被检查，
而不是在某一步被断言。`CATALOGUE_AUDIT_STEPS`：

```
declared_requirement → catalogue_existence → visible_to_model → compiler_accepts → renderer_supports
```

`CATALOGUE_AUDIT_IS_MACHINE_VERIFIABLE = True`，且
`CATALOGUE_AUDIT_ANCHOR_IS_DECLARED_REQUIREMENT = True`——
审计锚定在**用户请求里的要求**上，而不是锚定在目录碰巧有什么上。

`HIDDEN_IS_NOT_MISSING = True`：

- 存在但被可见性屏蔽、且 compiler/renderer 已支持 → **可见性缺陷**，解除屏蔽即可
- 目录里根本没有 → **目录缺口 `catalog_gap`**，必须显式报告

`CATALOGUE_GAP_FORBIDDEN_HANDLING` 三条禁令：

```
silent_omission                  ← 静默省略
substitute_a_lookalike_symbol    ← 拿长得像的符号冒充
invent_an_undefined_symbol_key   ← 发明一个不存在的 key
```

---

## 6. `catalog_gap` schema

`CATALOG_GAP_REQUIRED_FIELDS`：

```
requested_type · requested_tag · source_requirement · available_alternatives
```

- `CATALOG_GAP_IS_REPORTED_NOT_HIDDEN = True`
- `CATALOG_GAP_FORCES_COMPLETENESS = "partial"`——缺口让合成变**不完整**，而不是**非法**：
  计划本身没有错，只是目录表达不了其中一部分。
- `AUTO_GENERIC_SUBSTITUTION_ALLOWED_IN_PHASE_1 = False`。
  把一台专用设备自动降级成通用泵是**语义谎言**，本阶段明确推迟，而不是悄悄允许。
- `GENERIC_SUBSTITUTION_REQUIRES_EXPLICIT_UNMAPPED_MARKER = True`：
  将来若需要通用占位符，它必须明说"这里有一台尚未映射到标准符号的设备"，而不是假装分类已完成。

---

## 7. 画布：`DrawingLayoutIntent`，而不是 `set_canvas(width, height)`

**不批准让模型直接发原始画布操作。** 那会通过另一道门，把马上要从模型身上拿走的几何责任又还回去。

采用 `LAYOUT_INTENT_DATACLASS_NAME = "DrawingLayoutIntent"`，
`LAYOUT_INTENT_IS_DECLARED_NOT_COMPUTED = True`。`LAYOUT_INTENT_FIELDS`：

| 字段 | 允许值 | 必填 | 说明 |
|---|---|---|---|
| `orientation` | `landscape` / `portrait` | 是 | 提示词里"横向长图、不是方图"落在这里 |
| `preferred_aspect_class` | `standard` / `wide` / `extra_wide` | 是 | 是一个**类别**，不是比例、更不是像素 |
| `system_order` | `declared_system_ids_in_order` | 否 | 哪个系统在左/右/上/下 |
| `primary_flow_direction` | `left_to_right` / `right_to_left` / `top_to_bottom` / `bottom_to_top` | 是 | 主流程的阅读方向 |

最终画布**由确定性排版根据内容 bounds + margin 自动撑大**，不由模型计算 58273.8 × 4772.2。

---

## 8. 自动增长的画布

```
CANVAS_IS_DERIVED_FROM_CONTENT = True
CANVAS_GROWS_WHEN_CONTENT_EXCEEDS_CURRENT_BOUNDS = True
CANVAS_SHRINKS_WITHOUT_EXPLICIT_REQUEST = False
CANVAS_MARGIN_IS_REQUIRED = True
CONTENT_MAY_BE_CLIPPED_BY_CANVAS = False
EXTRA_WIDE_INTENT_SURVIVES_CONTENT_GROWTH = True   ← 不得靠只增高度来满足增长
```

`CANVAS_DERIVATION_INPUTS`：

```
content_bounds · required_margin · layout_intent.preferred_aspect_class · layout_intent.orientation
```

于是"横向长图"**变成可表达的**，但不是由 LLM 计算像素。

---

## 9. 职责划分：模型拥有意义，代码拥有几何

`MODEL_OWNS`：

```
equipment_inventory · equipment_classification · connectivity · system_membership
medium_pressure_diameter · instrument_requirements · redundancy_and_parallel_groups
required_closed_loops · engineering_grouping · layout_intent
```

`CODE_OWNS`：

```
absolute_coordinates · element_dimensions · canvas_bounds · system_partitioning
hierarchical_placement · orthogonal_routing · obstacle_avoidance · crossing_bridges
label_placement · leader_lines · visual_polish
```

两侧**必须是划分**（互不相交），重叠即测试失败。

模型输出的是 `DIAGRAM_SPEC_NAME = "DiagramSpec"`，
`MODEL_OUTPUT_IS_A_DIAGRAM_SPEC = True`，
`DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES = False`。

`DIAGRAM_SPEC_SECTIONS`：

```
systems · equipment · instruments · connections · required_loops · annotations · layout_intent
```

`FORBIDDEN_IN_MODEL_OUTPUT`——把这些名字逐个列出来，是为了让"重新把坐标责任还给模型"
变成一次**可见**的动作，而不是悄悄发生的：

```
absolute_x · absolute_y · element_width · element_height
canvas_width · canvas_height · pixel_margin · connector_waypoints
```

以及一条对现有机制的直接判决：

```
PRESERVE_POSITIONS_IS_VALID_FOR_LARGE_DIAGRAM_LAYOUT = False
```

---

## 10. 三个回归 fixture `ACCEPTANCE_FIXTURES`

Phase-1 **声明**它们；M7-1 的验收**运行**它们。

### fixture A — `partial_compile_is_not_success`

- 输入形状：一份每个操作各自合法、但累计状态拒绝了相当长尾部的计划（观测到的形状是
  197 proposed / 120 compiled / 77 rejected）
- 必须观察到：`completeness == partial`；`rejected_operation_count == 77`；
  每个被拒操作都有回执；会话**未**被记录为 completed；人能看到 proposed/compiled/rejected 三个数
- 必须不观察到：成功或"计划已通过校验"；completed 会话状态；被拒操作被静默丢弃

### fixture B — `unrepresentable_requirement_becomes_a_gap`

- 输入形状：请求里点名了目录无法表达的设备或仪表类别（例如分子筛吸附床，或没有对应符号的变送器类别）
- 必须观察到：`catalog_gap` 且给出 `requested_type` / `requested_tag` / `source_requirement`；
  `available_alternatives` 已枚举；`completeness == partial`
- 必须不观察到：静默省略；拿形似符号冒充；发明 symbol key；**用 indicator 冒充 transmitter**

### fixture C — `wide_multi_system_spec_is_expressible`

- 输入形状：多系统 + `landscape` / `extra_wide` 的 layout intent，内容 bounds 超出当前画布
- 必须观察到：layout intent 无需像素算术即可表达；画布增长到容纳内容 + margin；
  `extra_wide` 类别在增长后仍然保持
- 必须不观察到：内容被画布裁掉；模型给出画布宽高；增长只增加高度

---

## 11. Phase-1 边界

`PHASE_1_FORBIDDEN_SURFACES`（本阶段设计但不得构建）：

```
new_http_route · new_mcp_tool · new_ui_surface · diagram_spec_runtime
deterministic_layout_runtime · incremental_declaration_primitives
proposal_evidence_persistence
```

`DEFERRED_TO`：

| 工作 | 归属 |
|---|---|
| `semantic_first_deterministic_layout` | M7-2 |
| `incremental_declaration_primitives` | M7-3 |
| `coverage_driven_completion` | M7-3 |
| `raw_coordinate_responsibility_for_the_model` | never |

### M7-3 要用**语义**增量原语，不是低级增量原语

`M7_3_SEMANTIC_PRIMITIVES`：

```
declare_system · declare_equipment · declare_instrument
declare_connection · declare_required_loop · complete_subsystem
```

`M7_3_LOW_LEVEL_PRIMITIVES_FORBIDDEN`：

```
add_symbol_with_coordinates · add_connector_with_waypoints
```

每轮 agent 收到机器回执 `M7_3_RECEIPT_FIELDS`：

```
required · covered · rejected · catalog_gaps · isolated_equipment
unclosed_required_loops · duplicate_tags · unresolved_relations
```

只有**覆盖契约被满足**才允许宣告完成；不能再因为一次工具调用成功就把 session 标成 completed。

---

## 12. CAD 的定位

```
CAD_IS_A_RUNTIME_INPUT = False
CAD_ROLE = "offline_benchmark_reference"
```

对 M7 的产品运行时：**CAD 不是输入。** 用户给自然语言，产品画 P&ID。

`FORBIDDEN_FIDELITY_METRICS`：

```
semantic_element_count_divided_by_cad_primitive_count
```

理由（`FORBIDDEN_FIDELITY_METRIC_REASON`）：导入的 CAD 图纸是 9579 个**裸图元**，
没有符号、没有连接器；而合成结果是**符号模型**。两者不是同一种表示，
这个比值度量的是**表示法**，不是图纸。

正式基准应把 CAD 原图转化成人工审核过的**结构性验收项** `BENCHMARK_ACCEPTANCE_ITEMS`：

```
required_equipment · required_instruments · required_connections · required_closed_loops
required_systems · reference_zones · reference_layout_characteristics
```

CAD 可以作为视觉参考图保存，但不进入 M7 运行时输入路径。这不妨碍 M6 独立实现 CAD 语义摄取——
**两条产品能力不能混成一条。**

---

## 13. Phase-2A：把可诊断变成落库的运行时义务

Phase-1 只声明。Phase-2A（`Partial Synthesis Accountability`）把其中最紧要的一条变成代码，
本节是它对应的契约数据。

### 13.1 提案证据的载体是独立表，不是 tool call 的元数据

```
PROPOSAL_EVIDENCE_TABLE = "synthesis_proposal_evidence"
PROPOSAL_EVIDENCE_CARRIER = "dedicated_append_only_table"
PROPOSAL_EVIDENCE_MAY_LIVE_IN_TOOL_CALL_METADATA = False
PROPOSAL_EVIDENCE_IS_APPEND_ONLY = True
PROPOSAL_EVIDENCE_OVERWRITE_ALLOWED = False
```

理由是 proposal 与 tool call **不是一对一**：会话可能 propose → partial → replan →
propose → partial → replan → propose → complete → apply，只有最后一次尝试才可能对应一个
apply tool call。把证据塞进 tool-call 元数据，就会**再次**造成"只看得到最终那次调用、
看不到被拒的计划"——正是本次事故的结构。

`related_tool_call_id` 只是可选关联，
因此它既不是容器、也不承担生命周期与授权；没有 tool call 时证据同样成立。

### 13.2 提案记录的字段 `PROPOSAL_EVIDENCE_REQUIRED_FIELDS_PHASE_2A`

```
proposal_evidence_id · session_id · document_id · proposal_attempt_index
provider_class · model · planner_identity
raw_proposed_operations · proposed_operation_count
accepted_operation_count · compiled_operation_count · rejected_operation_count
rejected_operations · validity · completeness
operation_accounting · global_failure_reason
compiler_version · proposal_payload_digest · assessment_digest
related_tool_call_id · created_at
```

字段命名有一处刻意的区分：**`accepted_operation_count`** 是**被保留的语义操作**数（事故里是 120），
而 **`compiled_operation_count`** 保持它既有的含义——**产出的底层操作**数（事故里是 354，
因为一个被接受的语义操作会展开成符号、标签和引线）。因此远端写的
"proposed = compiled + rejected" 在这里落实为 **`proposed == accepted + rejected`**；
若把保留数也叫 compiled，就等于**默默改变了一个既有字段的含义**。

### 13.3 计数不变式 `PROPOSAL_COUNT_INVARIANTS`

```
proposed_operation_count == accepted_operation_count + rejected_operation_count
rejected_operation_count == len(rejected_operations)
completeness follows from the counts and is never supplied
a partial proposal has at least one rejection
a complete proposal has none
a proposal that was not evaluated carries no counts and no completeness verdict
```

`completeness` 必须是**推导出来的**：一个同时提供操作清单和完整性结论的调用方，
可以把丢掉的长尾描述成 complete。

三个值的判据是**互斥且递归的**：`accepted == 0` → `empty`（**判据在 accepted 侧**）；
否则 `rejected == 0` → `complete`；否则 `partial`。所以"提案 3 / 保留 0 / 拒绝 3"
是 **empty 而不是 partial**——它说的是"什么都没活下来"，而 rejected 非零告诉了我们
原因。这一点必须与旧版的 `0 / 0` 报告区分开：那里是**数字被伪造**，这里三个数字都是真的。

### 13.4 会话完成由后台拒绝 partial，而不只是界面显示

```
COMPLETED_SESSION_REQUIRES = ("valid", "complete")
COMPLETED_SESSION_IS_REFUSED_IN_BACKEND = True
COMPLETED_SESSION_REFUSAL_IS_UI_ONLY = False
```

一个从未画过任何对话框的调用方，也**不得**把部分计划记成 completed。

### 13.5 只读目录审计的判定值 `CATALOGUE_AUDIT_VERDICTS`

```
visible · hidden · missing · compiler_unsupported · renderer_unsupported
```

`CATALOGUE_AUDIT_CLASSIFIES_EXISTENCE_BEFORE_VISIBILITY = True`——
**先问存在性，再问可见性**，因为 hidden 说的是"存在但被压制"，missing 说的是"不存在"；
问反了就是二者被混为一谈的原因。审计只读：不新增符号、不改可见性。

### 13.6 「没评估」不是「空」：`OPERATION_ACCOUNTING_STATES`

```
OPERATION_ACCOUNTING_STATES = ("evaluated", "not_evaluated")
ACCOUNTING_STATE_IS_DECLARED_NOT_INFERRED = True
ZERO_IS_A_STAND_IN_FOR_UNKNOWN = False
SIX_CELL_MATRIX_APPLIES_TO = ("evaluated",)
NOT_EVALUATED_PROPOSAL_MUST_RECORD_WHY = True
EVIDENCE_PER_TERMINAL_ATTEMPT = 1
TERMINAL_PROPOSAL_WITHOUT_EVIDENCE_IS_ALLOWED = False
```

这是本轮最关键的一次口径修正。原先的规则是"没跑过 per-operation 编译器就不写证据行"，
它**与已签的 `RAW_PROPOSAL_IS_DURABLE_EVIDENCE = True` 直接冲突**：一次因 revision conflict
在逐项评估之前就中止的提交，恰恰是最需要留档的那次。但"写一行"不能靠伪造数字来换——
把 accepted/rejected 写成 `0`/`0` 是在用零冒充"未知"，和本里程碑要消除的那个缺陷同族。

所以两件事都不做：**不二选一，而是把状态显式化**。`operation_accounting` 声明这份提案是否被
逐项评估过；`not_evaluated` 时 `accepted_operation_count`、`rejected_operation_count`、
`completeness`、`rejected_operations` 一律为 `None`，并**必须**在 `global_failure_reason` 里
写明为什么停下。因此 `SIX_CELL_MATRIX_APPLIES_TO = ("evaluated",)`——
六格 `validity × completeness` 矩阵只描述被评估过的提案，这样就不必再给 completeness
发明第四个值（"unknown"），也不会把"没评估"说成 `empty`。

同理，`EVIDENCE_PER_TERMINAL_ATTEMPT = 1`：每个被系统终态处理的提案尝试**恰好**留下一行——
不是零行（那是事故里 77 个被丢操作不可见的机制），也不是多行。

### 13.7 partial 必须真的走一遍"回执 → 重规划"

```
PARTIAL_PROPOSAL_REQUESTS_NEXT_ATTEMPT = True
PARTIAL_REPLAN_USES_EXISTING_LIMIT = True
```

Phase-1 签下的是"partial 回执交回 agent → 重规划 → 只有 valid+complete 能继续"。
只把 `may_proceed_to_authorisation()` 变成 `False` 会停在"**不能完成，但也不会按回执自动修**"，
比"静默成功"更难看：真实自动路径的循环条件若只看 `valid`，`valid=true, completeness=partial`
时它不会重规划。所以循环条件改成"以 `may_proceed_to_authorisation()` 为准"，
被拒操作的**回执被放进重规划上下文**（`rejected_operations` 随 `failure` 一起序列化进 prompt），
并且**复用既有的重规划上限**，本轮不重造调度器。

### 13.8 确认界面必须显示这笔账

```
COMPLETENESS_PRESENTATION_FIELDS = ("proposed_operation_count", "accepted_operation_count",
                                    "rejected_operation_count", "completeness")
PARTIAL_PROPOSAL_MAY_RENDER_AS_PASSED_VALIDATION = False
PARTIAL_PROPOSAL_MAY_OFFER_APPLY = False
```

这三条不是"界面大改版"，只是把 Phase-1 已经签下的事实呈现出来：既然确认框里写着
"计划已通过校验"，那么 `197 提案 / 120 接受 / 77 拒绝 / partial` 就必须同屏出现，
并且 partial 时**不得**出现"通过校验"的措辞、也不得出现正常的确认/应用按钮。
把 completeness 藏起来而只显示 `valid=true`，正是原始事故在界面上的样子。

### 13.9 「没评估」与「响应违反了契约」是两条路

```
ACCOUNTING_AXIS_FIELD = "operation_accounting"
ASSESSMENT_CONTRACT_VIOLATION_CODE = "assessment_contract_violation"
ASSESSMENT_CONTRACT_VIOLATION_IS_A_BUSINESS_OUTCOME = False
CONTRACT_VIOLATION_MAY_APPLY = False
CONTRACT_VIOLATION_MAY_TRIGGER_AUTOMATIC_SEMANTIC_REPLAN = False
CONTRACT_VIOLATION_REQUIRES_USER_VISIBLE_MESSAGE = True

EVALUATED_ASSESSMENT_REQUIRED_FIELDS     = (accepted_operation_count, rejected_operation_count,
                                            completeness, rejected_operations)
NOT_EVALUATED_ASSESSMENT_REQUIRED_FIELDS = (global_failure_reason,)

ASSESSMENT_BRANCHES = (evaluated_complete · evaluated_partial · evaluated_empty ·
                       evaluated_invalid · not_evaluated · assessment_contract_violation)
CONTINUATION_ACTION_BRANCH = (proceed_to_human_confirmation → evaluated_complete ·
                              return_receipt_and_replan → evaluated_partial ·
                              replan_or_abort → evaluated_empty ·
                              existing_error_recovery → evaluated_invalid)
```

这是最容易"顺手合并"的一处，而合并的代价是不对称的：

```
evaluated + valid + complete   → 人工确认
evaluated + valid + partial/empty → 带回执的语义重规划
evaluated + invalid            → 既有错误恢复
not_evaluated                  → 既有错误恢复，用 global_failure_reason（可换上下文后重新规划）
缺 operation_accounting / evaluated 却缺 counts·completeness
                               → assessment_contract_violation：不 apply、不自动语义重规划、给人可见提示
```

**`not_evaluated` 是服务端的一个真实回答**（"没走到逐项评估"），它带 `global_failure_reason`，
按它恢复是合理的；**缺字段或自相矛盾是响应坏了**，那不是能靠再要一份提案修好的东西——
把它当 `not_evaluated` 吞掉，等于用 5 次模型请求去掩盖一个契约缺陷。所以最后一条**既不 apply，
也不允许自动语义重规划**，只给"响应契约不兼容，请刷新或重新生成"这一类可执行的提示。

"缺字段"不是第四种 accounting 状态：`ACCOUNTING_AXIS_FIELD` 缺失即契约违规。同理，
`evaluated` 却缺计数/完整性、或 `not_evaluated` 却带着计数，都属于同一类违规，都不得被
当成 `partial`/`empty` 继续走下去。六个分支是穷尽的，且**恰好一个分支既不可 apply 也不可重规划**
（就是那一支），这些都由 `validate_contract()` 与绑定测试逐条钉住。

`(session_id, proposal_attempt_index)` 的唯一约束是同一件事的存储侧：`proposal_attempt_index`
承担审计顺序，就不该允许同一个 session 出现两个"第 2 次提案"（schema v10 加唯一索引；
v9 的定义一个字不改，因为这台机器的真实库已经执行过 v9）。

**`global_failure_reason` 必须随 `assessment` 一起上线，而不是只写在证据行里。**
这不是实现细节，是上面那条分支能不能成立的前提：客户端要区分"服务端说没评估、并给了原因"
与"响应压根没带契约"，就只能从响应本身读到原因。我是写这条边界测试时才发现它缺失的——
`/agent/plan-v2` 的响应里没有这个字段，于是真实的 `not_evaluated`（revision conflict）
会被新前端误判为契约违规。所以它现在是 assessment 上的一等字段，且
`not_evaluated` 而原因为空本身就是一条可报错的违规。
