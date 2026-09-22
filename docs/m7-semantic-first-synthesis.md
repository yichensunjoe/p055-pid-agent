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
