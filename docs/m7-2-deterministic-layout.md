# M7-2 — Semantic-First Deterministic Layout

> 本文件是 M7-2 的任务书。契约是数据，写在 `backend/agentcad/m7_layout_contract.py`；
> 数据与本文由 `backend/tests/test_m7_layout_contract.py` 逐条对拍：**凡是契约里声明的名字，
> 本文必须出现**，否则测试报错。契约版本 `m7-layout-contract/1`。

## 0. 本阶段分两段，Phase-1 不写运行时

```
M7-2 Phase-1   Design & Contract
M7-2 Phase-2   Runtime Integration
```

Phase-1 **不得**新增真正的 `diagram_spec_runtime`、`deterministic_layout_runtime`、`layout_service`，
也不得新增任何 HTTP/MCP/UI 表层，也不得改变现有生产绘图行为。
`PHASE_1_MAY_CHANGE_PRODUCTION_DRAWING_BEHAVIOUR = False`。

本阶段接手的是 M7 线留下的延期项：`SUPERSEDES_DEFERRAL = ("semantic_first_deterministic_layout",)`，
`DEFERRED_TO_PHASE = "M7-2"`。M7 契约里那条 `DEFERRED_TO` 仍然写着 M7-2，
所以两份契约是**指向关系**而不是互相矛盾（绑定测试会读 M7 契约确认这一点）。

## 1. 为什么做这件事：M7-2 要拿走的是"模型负责摆位置"

M7 Phase-2A 解决的是"没画完要说出来"。剩下的一半是：**那次事故里 80% 的输出字节是坐标与样式**，
而系统里本来就有一个确定性排版引擎，却以 `preserve_positions=True` 被调用——
等于明确告诉它"不要摆任何东西"。于是"画一张厂区级总图"的上限不是**含义**能装下多少，
而是**坐标对**能装下多少。

`MODEL_OWNS` 是模型该负责的：

```
meaning · systems · equipment · connections · required_loops · layout_intent
```

`CODE_OWNS` 是代码该负责的：

```
system_partition · rank_assignment · absolute_placement · spacing
orthogonal_routing · obstacle_avoidance · annotation_placement · canvas_bounds
```

两条禁令是这一步的验收尺：

```
MODEL_OUTPUT_MAY_CONTAIN_ABSOLUTE_GEOMETRY = False
MODEL_OUTPUT_MAY_CONTAIN_RELATIVE_ANCHORS    = False
DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES    = False
LAYOUT_ENGINE_IS_SINGLE_AUTHORITY            = True
```

`DIAGRAM_SPEC_DECLARES`（图规格能声明的东西，别的都不是它的一部分）：

```
system · equipment · instrument · connection · required_loop · layout_intent
```

### 1.1 模型不得输出的几何字段

```
x · y · position · points · waypoints · width · height
canvas_width · canvas_height · rotation · start · end · center · radius
port_x · port_y
```

这些名字**作为排版输出完全合法**——方向才是问题，不是拼写：
坐标从引擎流向画布是目的，坐标出现在模型输出里才是缺陷。
`FORBIDDEN_MODEL_GEOMETRY_FIELDS_ARE_ALLOWED_AS_LAYOUT_OUTPUT = True`。

## 2. `preserve_positions` 按路径分流，不是全仓删除

```
M7_SYNTHESIS_USES_PRESERVE_POSITIONS      = False
LEGACY_MANUAL_LAYOUT_MAY_PRESERVE_POSITIONS = True
LEGACY_PRESERVE_POSITIONS_PATHS = (human_edited_drawing, local_reroute, legacy_manual_editing)
```

规则是作用在**路径**上而不是仓库上："M7 语义合成路径必须为假"，而人工已经摆过的图、
局部 reroute、旧的编辑场景**可以**为真。为了 M7 去破坏既有编辑场景不是进步。

## 3. 画布是排版的结果，不是请求的参数

```
CANVAS_IS_LAYOUT_OUTPUT = True
AUTO_LAYOUT_REQUEST_MAY_TAKE_CANVAS_PIXELS = False
AUTO_LAYOUT_REQUEST_FORBIDDEN_PARAMETERS = (canvas_width, canvas_height)
AUTO_LAYOUT_REQUEST_DISCRETE_CONSTRAINTS = (layout_intent, system_order,
                                            primary_flow_direction, grouping)
AUTO_LAYOUT_OUTPUT_ADDS = (content_bounds, canvas_bounds, canonical_layout_digest)
CANVAS_GROWS_TO_FIT_CONTENT = True
CANVAS_MARGIN_IS_DECLARED_NOT_ASSUMED = True
ASPECT_CLASS_SURVIVES_GROWTH = True
```

把 `canvas_width` / `canvas_height` 做成请求参数，等于再造一个画布权威——
而那正是 1600×900 默认值的来源：一张 12:1 的厂区图**在工具面上无法表达**。
请求只表达意图（例如 `extra_wide`），像素由引擎推导。`ASPECT_CLASS_SURVIVES_GROWTH` 是一条
容易漏掉的正向规则：画布为了装下内容而变大之后，`extra_wide` 必须仍然成立，否则那条意图只是装饰。

## 4. `layout_intent`：Phase-1 只允许离散意图

| 维度 | 取值 | 说明 |
|---|---|---|
| `orientation` | `landscape` · `portrait` | 读图方向，不是尺寸 |
| `preferred_aspect_class` | `standard` · `wide` · `extra_wide` | 参考图纸需要的是 `extra_wide`（约 12:1） |
| `primary_flow_direction` | `left_to_right` · `top_to_bottom` | 主流程走向，rank 跟着工艺顺序 |
| `system_order` | 开放（`open_ended`，按声明过的 system id 校验） | 明确的分区顺序 |
| `grouping` | `grouped_by_system` · `grouped_by_zone` · `flat` | 同一系统放在一起是组织性陈述 |
| `density` | `compact` · `comfortable` | 间距等级，一次说清而不是逐个数挪 |

**v1 禁止自由相对锚点**：`FREE_RELATIVE_ANCHORS_ALLOWED_IN_V1 = False`，
禁止谓词是：

```
left_of · right_of · above · below · near · closer_than · distance_ratio
```

原因不是"相对关系都错"，而是自由相对锚点会迅速演变成另一种**几何编程语言**，
把排版责任从侧门还给模型。将来若工程上确实需要，应引入的是**语义邻接约束**
（表达拓扑与组织关系，而不是位置）：

```
FUTURE_SEMANTIC_ADJACENCY_CONSTRAINTS = (same_system, upstream_of, downstream_of,
                                         keep_together, separate_groups)
```

## 5. 排版不得改动工程语义（M7-2 最重要的负向 Gate）

```
LAYOUT_MAY_CHANGE_TOPOLOGY = False
LAYOUT_MAY_CREATE_OR_DELETE_ENGINEERING_EQUIPMENT = False
LAYOUT_MAY_CHANGE_TAGS = False
LAYOUT_MAY_CHANGE_SYSTEM_MEMBERSHIP = False
SEMANTIC_DIGEST_BEFORE_LAYOUT_MUST_EQUAL_SEMANTIC_DIGEST_AFTER = True
```

即：

```
排版前的 DiagramSpec 语义摘要 == 从排版结果重建出来的语义摘要
```

排版**允许**新生的只有呈现产物：

```
LAYOUT_MAY_CHANGE_PRESENTATION_ONLY = (waypoints, annotation_positions,
                                       leader_lines, crossing_presentation, canvas_bounds)
```

`ENGINEERING_SEMANTIC_NOUNS = (connections, equipment, instruments, tags, systems, topology)`
——这些词不得出现在上面那份"允许改变"的清单里。为了"更好画"偷偷删掉一条 connection、
或把一个设备类别改掉，是这条 Gate 要拦的东西。

## 6. 确定性：canonical layout digest 的定义

```
LAYOUT_IS_DETERMINISTIC = True
LAYOUT_DIGEST_VERSION     = "m7-layout-digest/1"
LAYOUT_PROJECTION_VERSION = "m7-layout-projection/1"
LAYOUT_DIGEST_INPUTS = (layout_digest_version, layout_projection_version,
                        diagram_spec_semantic_digest, layout_engine_version,
                        layout_rules_version, canonical_projection_envelope,
                        canonical_placement_projection)
LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING = (created_at, duration_ms, iteration_count,
                                               provider, model, session_id, plan_id,
                                               attempt, layout_run_id)
PROJECTION_CHANGE_REQUIRES_VERSION_BUMP = True
NUMERIC_CANONICALIZATION_CHANGE_REQUIRES_VERSION_BUMP = True
COORDINATE_QUANTUM_CHANGE_REQUIRES_VERSION_BUMP = True
SORT_KEY_CHANGE_REQUIRES_VERSION_BUMP = True
VERSION_BUMP_IS_EXPLICIT_NOT_IMPLIED = True
```

**摘要与投影各自版本号**，因为 `layout_engine_version` / `layout_rules_version` 描述的是
**跑过的引擎**，而不是**被摘要的东西的形状**。没有这两个版本号时，加一个投影字段或换一套数值规范化
会让**另一份摘要顶着同一个名字**——那正是本里程碑要消除的身份缺陷，只是降了一层。
`DIGEST_VERSION_CONTRACT` 把"版本号"与它所指的规则绑在一起（字段集、包络、排序键、
数值规范化、quantum）：**改了其中任何一项而不升版本，校验器直接报错**。

> 同一份 DiagramSpec + 同一 layout engine version + 同一 layout rules version
> → 同一个 canonical layout digest

摘要**逐字段**定义，因为"canonical"否则只是一个词：`CANONICAL_LAYOUT_PROJECTION_FIELDS`
把每个字段和它是否进入摘要都写下来。**行字段**（每个实体一行）：

```
engineering_id（稳定工程身份，改名不改摘要）
placement_kind（设备/标注/走线的放置类别，防止"碰巧相等"）
x · y · width · height
ordered_waypoints（按遍历顺序，才可能比较两条走线）
```

**包络字段**（整张图一个，不重复在行上）：

```
CANONICAL_PROJECTION_ENVELOPE_FIELDS = (content_bounds, canvas_bounds)
BOUNDS_ARE_ENVELOPE_FIELDS_NOT_ROWS = True
```

边界是全局事实：把它重复在每一行上，一个全局值就变成了"依赖行的碰巧顺序"的值。
如果将来一个工程实体确实对应多条呈现行，补上 `CANONICAL_PROJECTION_PRESENTATION_ROLE_FIELD`
（`presentation_role`），扩展是**声明式**的而不是"不小心重复".

不进入投影的（易变记账）：

```
duration_ms · created_at · layout_run_id
```

把 `duration_ms` 这类字段放进摘要，会让**正确的重放看起来像一次改动**——
这和 A5 语料身份里"把运行期 volatile 混进身份"是同一个错。

### 6.1 数值规范化：浮点相等不是身份

```
LAYOUT_NUMERIC_CANONICALIZATION = finite_fixed_decimal_v1
LAYOUT_COORDINATE_DECIMALS = 6
LAYOUT_COORDINATE_QUANTUM  = 1e-6
COORDINATE_QUANTUM_IS_DECLARED_NOT_DERIVED = True
NUMERIC_CANONICALIZATION_REJECTS_NON_FINITE = True
NEGATIVE_ZERO_IS_NORMALIZED_TO_ZERO = True
CANONICAL_SERIALIZATION_IS_REPR_INDEPENDENT = True
CANONICAL_SERIALIZATION_IS_LOCALE_INDEPENDENT = True
CANONICAL_SERIALIZATION_IS_TIME_INDEPENDENT = True
EQUAL_CANONICAL_VALUES_PRODUCE_EQUAL_BYTES = True
LAYOUT_NUMERIC_CANONICALIZATION_RULES = (reject_nan, reject_positive_infinity,
    reject_negative_infinity, normalize_negative_zero_to_zero,
    quantize_to_declared_coordinate_quantum, format_without_python_repr,
    format_without_locale, no_timing_or_environment_input,
    equal_canonical_values_must_produce_equal_bytes)
```

六位小数与绘图管线**已经在用**的精度一致（`round(..., 6)`，见 `drafting_geometry`）。
关键是它是**契约常量**：从 `grid_size`、画布或运行环境推出来的精度，会让摘要取决于
**正在画的那张图**。`-0` 必须规范成 `0`，否则同一个位置有两个摘要；
NaN/±Inf 必须**硬拒绝**而不是参与排序。

### 6.2 total order 必须由数据结构证明

```
CANONICAL_PROJECTION_IS_SORTED = True
CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE = True
CANONICAL_PROJECTION_SORT_KEY = (placement_kind, engineering_id)
CANONICAL_PROJECTION_SORT_KEY_IS_COMPOSITE = True
CANONICAL_PROJECTION_SORT_KEY_IS_UNIQUE = True
DUPLICATE_SORT_KEY_IS_HARD_FAIL = True
CANONICAL_PROJECTION_IS_TOTAL_ORDERED = True
CANONICAL_PROJECTION_TOTAL_ORDER_IS_PROVEN_BY_UNIQUENESS = True
```

单字段 `engineering_id` **不能**证明全序：不同类型完全可能携带同一个 engineering id，
那样"canonical 顺序"就取决于插入顺序。所以排序键是复合的，而且投影必须在它上面**唯一**
（重复排序键 = hard fail）。关键不是一定要三列，而是 **total order 由数据结构证明，
不由布尔常量宣称**。

## 7. 组件职责：适配层不是第二个排版器

`LAYOUT_COMPONENTS = (DiagramSpecAdapter, AutoLayoutEngine)`。

| 组件 | owns | must_not_own |
|---|---|---|
| `DiagramSpecAdapter` | `spec_to_topology` · `semantic_identity_preservation` · `layout_intent_translation` | `x` · `y` · `absolute_placement` · `orthogonal_routing` · `canvas_bounds` |
| `AutoLayoutEngine` | `CODE_OWNS` 全部八项 | `meaning` · `topology` · `tags` · `system_membership` |

适配层只做"含义 → 引擎可消费的拓扑输入"，**不决定 x/y、不走线、不算画布**。
排版权威只有 `AutoLayoutEngine` 一个：两份放置真相是"东西该放哪"的两个答案，
而正是这一类缺陷把这张图弄坏的。

## 8. 验收 fixture：四类，不是三类

| key | name | 输入形状 | 必须观察到 | 必须不观察到 |
|---|---|---|---|---|
| **A** | `small_semantically_complete_diagram` | 少量 system/equipment/connection + 一条必闭合回路 | 每个声明实体都被放置；每条声明连接都有走线；没有实体停在默认原点 | 规格里出现任何坐标；实体被放到推导出的画布之外 |
| **B** | `multi_system_plant_with_required_loop` | 多系统、大量设备、含必须闭合的回路，接近参考厂区规模 | 系统分组保持；主流向跟随工艺顺序；回路拓扑不被排版改写；画布增长到装下内容 | 被丢弃或合并的 connection；被改类别的设备；内容被画布裁掉 |
| **C** | `forbidden_geometry_input` | 带坐标、相对锚点或画布像素的规格 | 几何被拒绝；拒绝信息指出违规字段 | 静默接受坐标；静默剥离坐标；从被拒规格产出半成品排版 |
| **D** | `determinism` | 同一份规格连续排版两次 | canonical layout digest 完全一致；排版前后语义摘要一致 | canonical projection 出现任何差异；摘要里出现任何易变记账字段 |

A 与 B 的分工是"能不能画"与"能不能画全且不改语义"；C 与 D 是两条独立的负向断言
（**该拒的要拒**、**该等的要等**），一条 fixture 无法诚实地同时证明这两件事。

## 9. Phase-1 边界

`PHASE_1_FORBIDDEN_SURFACES`：

```
new_http_route · new_mcp_tool · new_ui_surface · diagram_spec_runtime
deterministic_layout_runtime · layout_service · canvas_parameter_on_auto_layout_request
```

`PHASE_1_FORBIDDEN_SURFACE_TOKENS`（表层测试用它们扫现存路由与 MCP 工具）：

```
diagram_spec · diagram-spec · layout_intent · layout-intent
deterministic-layout · deterministic_layout
```

### 9.1 已经存在的排版表层：`auto_layout` 不是违规词

写表层测试时发现的真事：`PRE_EXISTING_LAYOUT_SURFACES = (preview_auto_layout, apply_auto_layout)`
**本来就在**（确定性制图那条线的 MCP 工具，对应 `/drafting/*` 与 `/layout/preview` 路由）。

所以第一版把 `auto_layout` 当违规 token 是错的：它会把**既有**的人工排版路径报成 M7-2 违规，
而真正的违规被埋在一堆假阳里。这与 `preserve_positions` 的分流是同一件事在表层的投影——
那两条 MCP 工具正是**legacy/manual 路径**，它们**可以** preserve positions；
Phase-1 不许改它们，也不许添第三个。契约里因此多一条规则：

```
违规 token 不得是任何既有排版表层名的子串  ← 否则它报的是 legacy，不是新表层
```

Phase-1 能断言的剩下那件事是：这个集合**没有变大**（多一条 auto-layout 表层就会被抓住）。

Phase-1 的产物只有三件：契约数据、本任务书、绑定测试。**运行时不写，行为不改。**

### 9.2 版本治理的能力边界（说清楚，不要为此再搭一层）

```
VERSION_CHECK_DETECTS_DRIFT_BETWEEN_LIVE_AND_FROZEN_DEFINITION = True
VERSION_CHECK_CANNOT_PREVENT_A_DELIBERATE_DOUBLE_EDIT = True
```

任何"规则变了就必须升版本"的机器校验，本质上只能检测**当前声明与冻结定义的漂移**：
`DIGEST_VERSION_CONTRACT` 是那份冻结定义，`validate_contract()` 把它和 live 声明对拍。
如果将来有人**两边同时改、并故意保留旧版本字符串**，代码拦不住——那要靠 code review 与 Gate。
这一条写进契约，就是为了**不再为"让代码无法修改自己的规则"这个不可能的目标再搭一层复杂机制**。
目前这套 contract + mutation + Release Gate 是足够的。

---

## 10. M7-2 Phase-2A — DiagramSpec Adapter

运行链**到此为止**：

```
DiagramSpec → validate → DiagramSpecAdapter → deterministic semantic topology input → STOP
```

`PHASE_2A_MAY_BUILD`：

```
diagram_spec_runtime · diagram_spec_adapter · semantic_topology_input · adapter_topology_digest
```

`PHASE_2A_FORBIDDEN`（本阶段禁止）：

```
absolute_geometry_generation · width_height_generation · waypoint_generation
routing · annotation_placement · canvas_calculation
auto_layout_engine_placement_changes
new_http_route · new_mcp_tool · new_ui_surface
```

### 10.1 适配层的映射表

| 输入（`DIAGRAM_SPEC_DECLARES`） | 输出 |
|---|---|
| `system` | `topology_systems` |
| `equipment` | `topology_nodes` |
| `instrument` | `topology_instrument_nodes_and_relations` |
| `connection` | `topology_edges` |
| `required_loop` | `preserved_loop_constraints` |
| `layout_intent` | `discrete_engine_constraints` |

`ADAPTER_MUST_PRESERVE`（一律**原样**保留，不得重命名、不得重分类、不得重新归属）：

```
engineering_ids · tags · system_membership · topology · required_loops
```

### 10.2 适配层不得输出几何

```
ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY = False
ADAPTER_OUTPUT_CONTAINS_WAYPOINTS = False
ADAPTER_OUTPUT_CONTAINS_CANVAS = False
ADAPTER_REJECTS_MODEL_GEOMETRY_BEFORE_VALIDATION = True
ADAPTER_MAY_SILENTLY_STRIP_GEOMETRY = False
```

`load_diagram_spec` 先用 `reject_geometry` 扫**原始 payload**，再交给模型校验；违规抛
`DiagramSpecGeometryError`，并**在错误里指名违规字段**（例如 `$.entities[0].x`）。
扫原始 payload 而不是已解析的模型，是因为字段白名单只能拒绝它知道的键；扫原始映射还能抓住
实体内部、回路内部、以及将来新字段里的几何。

**不得静默 strip 掉坐标继续跑**：那会让一个违反契约的模型响应看起来成功——
这正是本里程碑要消除的那类缺陷，不能在另一个位置重演。相对锚点（`left_of` / `near` /
`relative_to` / `anchor` / `offset` …）与坐标同罪：它们作为**键名**或作为**自由文本里的整词**
都会被拒绝（整词匹配，避免误杀正常叙述）。

### 10.3 适配层自己的摘要（与 layout digest 分开）

```
ADAPTER_IS_DETERMINISTIC = True
ADAPTER_SEMANTIC_DIGEST_BEFORE_EQUALS_AFTER = True
ADAPTER_TOPOLOGY_DIGEST_IS_NOT_THE_LAYOUT_DIGEST = True
ADAPTER_TOPOLOGY_DIGEST_VERSION = "m7-adapter-topology-digest/1"
ADAPTER_TOPOLOGY_DIGEST_INPUTS = (adapter_topology_digest_version, adapter_topology_projection)
ADAPTER_TOPOLOGY_DIGEST_SORT_KEY = (kind, engineering_id)
ADAPTER_TOPOLOGY_PROJECTION_FIELDS = (kind · engineering_id · system_id · tag · name ·
    equipment_class · instrument_type · measurement · ports · medium · source_engineering_id ·
    target_engineering_id · source_port_id · target_port_id · engineering_ids · orientation ·
    preferred_aspect_class · primary_flow_direction · grouping · density · system_order)
ADAPTER_TOPOLOGY_DIGEST_VERSION_FIELD_SET = 同上一行（版本号钉住字段集）
```

为什么适配层要自己的摘要：Phase-2B 出现"两次排版不一样"时，需要有东西能先回答
**差异来自适配层还是来自排版引擎**。它**不是** `canonical_layout_digest`，也不进入后者的输入
（`ADAPTER_TOPOLOGY_DIGEST_VERSION not in LAYOUT_DIGEST_INPUTS`）。

两条可断言的性质：

```
adapter 前后语义摘要相等          （无损：不能掉一条连接、不能改一个设备类别）
同一 DiagramSpec → 同一 topology 摘要   （确定性：调用方列出的顺序不进入身份）
```

### 10.4 可导入契约的模块（白名单，写在契约里）

```
PHASE_2A_MAY_IMPORT_THE_CONTRACT = (m7_diagram_spec.py, m7_diagram_adapter.py)
```

Phase-1 曾断言"没有任何 agentcad 模块 import 这份契约"（被运行时引用的契约就是运行时）。
Phase-2A 落运行时后，这个断言变成白名单：**只有适配层那两个模块**可以读契约——
其余模块一旦 import 就会被测试抓住。

### 10.5 下一阶段（本阶段不做）

| 阶段 | 名称 | 内容 |
|---|---|---|
| **M7-2 Phase-2B** | **Deterministic Layout Engine Integration** | 让 `AutoLayoutEngine` 真正接管 placement / routing / canvas bounds 与最终 `canonical_layout_digest`；主要用 fixture B/D 与确定性重放 |

Phase-2A 不解决"大图怎么排"。拓扑难排、系统多、required loop 复杂都不是本阶段的失败；
本阶段只证明一件事：**模型已经彻底失去 geometry authority，而所有工程语义完整、确定性地抵达了引擎门口。**

---

## 11. M7-2 Phase-2B — Deterministic Layout Engine Integration

Phase-2A 把模型赶出了几何领域，并在引擎门口停下。Phase-2B 走完剩下那一段：**让那份
`SemanticTopology` 真正进入唯一的排版权威，并产出可复现的 canonical layout digest。**

### 11.1 接缝：不新增第二个有语义权力的适配层

不采用：

```
DiagramSpec → DiagramSpecAdapter → SemanticTopology → 又一个聪明的 TopologyToEngineAdapter → AutoLayoutEngine
```

那会重新造出两个"谁决定排版输入"的真相源。采用：

```
DiagramSpec → DiagramSpecAdapter → SemanticTopology → AutoLayoutEngine semantic-topology ingress
```

```
ENGINE_SEMANTIC_TOPOLOGY_INGRESS = layout_semantic_topology
SEMANTIC_TOPOLOGY_IS_THE_ENGINE_FACING_INPUT_CONTRACT = True
SECOND_ADAPTER_WITH_SEMANTIC_AUTHORITY_IS_ALLOWED = False
LAYOUT_AUTHORITY_COUNT = 1
LEGACY_INGRESS_RETAINS_THE_SAME_ALGORITHM_AUTHORITY = True
```

`SemanticTopology` **就是** M7-2 的 engine-facing semantic input contract。

引擎内部可以有一个纯结构 normalization（把 `TopologyNode` / `TopologyEdge` 变成它既有算法
使用的内部 graph object），但这层只能是**表示转换**，三个零是它全部的授权：

```
INTERNAL_NORMALIZATION_IS_REPRESENTATION_ONLY = True
INTERNAL_NORMALIZATION_SEMANTIC_DECISIONS = 0
INTERNAL_NORMALIZATION_GEOMETRY_DECISIONS = 0
INTERNAL_NORMALIZATION_TOPOLOGY_EDITS = 0
```

最合适的形状是方法，而不是组件：

```python
AutoLayoutEngine.layout_semantic_topology(topology: SemanticTopology, ...) -> LayoutResult
```

**禁止为兼容旧入口给节点塞假坐标：**

```
ENGINE_INGRESS_MAY_FABRICATE_PLACEHOLDER_POSITIONS = False
ENGINE_INGRESS_MAY_DISGUISE_TOPOLOGY_AS_A_POSITIONED_DOCUMENT = False
```

否则坐标责任只是从模型转到适配层，然后由引擎 preserve / 修正——旧问题换个帽子回来。
legacy/manual 的既有入口继续存在，两条路径最终共用**同一个** `AutoLayoutEngine` 算法权威。

### 11.2 `preserve_positions` 不是模型的 `layout_intent`

`LAYOUT_INTENT_DIMENSIONS` 仍然**只有六个**：`orientation`、`preferred_aspect_class`、
`primary_flow_direction`、`system_order`、`grouping`、`density`。`preserve_positions` 属于
**执行路径 policy**，不属于模型可声明的意图：

```
M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS = False
M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER = True
M7_INGRESS_PRESERVE_POSITIONS_IS_CALLER_OVERRIDABLE = False
```

执行方式是**签名里没有这个参数**，而不是"传了再拒绝"：调用方无从覆盖。契约校验器另外声明
**任何名为 `preserve_positions` 的 intent 维度都是违规**——模型不能通过
`"preserve_positions": true` 重新取得几何权力。

### 11.3 离散 `layout_intent` 从 ingress 第一刀起就被消费

六个维度**都在 Step 1 被接收**，并且每一个都有明确的施加位置——这就是
`LAYOUT_INTENT_CONSUMPTION`（`IntentConsumption(dimension, received_at_step, applied_at_step, behaviour)`）：

| 维度 | 接收 | 施加 | 作用 |
|---|---|---|---|
| `orientation` | step_1 | step_4 | 画布方向在派生的画布上强制，而不是以像素请求 |
| `preferred_aspect_class` | step_1 | step_4 | 形状类别塑形派生画布，使 `extra_wide` 可达 |
| `primary_flow_direction` | step_1 | step_2 | 排序跟随工艺顺序，图才按工艺方向阅读 |
| `system_order` | step_1 | step_2 | 系统分区按声明序列，且对 spec 校验 |
| `grouping` | step_1 | step_2 | 分组或平铺放置 |
| `density` | step_1 | step_2 | 间距类别，一次作为间距使用，而不是逐元素微调 |

```
INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP = step_1
UNIMPLEMENTED_INTENT_DIMENSION_MAY_BE_SILENTLY_IGNORED = False
```

**已声明但引擎尚未实现的维度，不得被静默忽略。** Phase-2B 最终 Gate 时，六个维度都必须有
消费位置与测试。反向也成立：一个没有消费点的维度、或一个指向不存在维度的消费点，都会被
校验器报出来。

内部实施顺序（`PHASE_2B_STEPS`）：

```
step_1  semantic_topology_ingress_and_intent_resolution
step_2  deterministic_rank_and_absolute_placement
step_3  orthogonal_routing_and_annotation_placement
step_4  content_bounds_to_canvas_bounds_and_aspect_enforcement
step_5  canonical_projection_and_layout_digest
```

`orientation` / `preferred_aspect_class` 对画布的最终效果在 Step 4，但它们从 Step 1 起就作为
**已知约束**存在——不能被静默忽略，最后再补一个宽画布。

### 11.4 Canvas 属于 Phase-2B 的 engine-output 尾段

```
CANVAS_DERIVATION_CHAIN = (
  semantic_topology → system_partition → absolute_placement → routing → annotations
  → content_bounds → derive_canvas_bounds_from_content_and_margin_and_intent
  → verify_no_clipping → canonical_layout_projection → canonical_layout_digest )
CANVAS_IS_ENGINE_OUTPUT_DERIVED_FROM_CONTENT = True
CANVAS_MUST_BE_VERIFIED_TO_CLIP_NOTHING = True
ENGINE_MAY_TAKE_INTENT_AND_MARGIN_POLICY = True
ENGINE_MAY_TAKE_CANVAS_DIMENSIONS_AS_INPUT = False
```

`canvas_width` / `canvas_height` **仍然不得进入 `AutoLayoutRequest`**。引擎可以接收
`orientation`、`preferred_aspect_class` 与 margin policy/version；**画布尺寸本身只能是输出。**

### 11.5 本阶段的表层边界

```
PHASE_2B_WIRES_THE_INGRESS_TO_ANY_SURFACE = False
PHASE_2B_MAY_IMPORT_THE_CONTRACT = (auto_layout_semantic.py)
ENGINE_INGRESS_REPORTS_ENGINE_AND_RULES_VERSION = True
ENGINE_VERSION_CONSTANT_NAMES = (LAYOUT_ENGINE_VERSION, LAYOUT_RULES_VERSION)
```

引擎不会变成组件，`auto_layout_semantic.py` 也不是第二个引擎：ingress 以组合/混入的方式
落在**既有** `AutoLayoutEngine` 类上。digest 需要 `layout_engine_version` 与
`layout_rules_version` 两个输入，契约声明引擎**必须发布的常量名**，值由引擎自己持有——
规则变化因此是带可见版本号的引擎改动，而不是契约改动。本阶段仍然**不加任何表层**：
没有路由、没有 MCP 工具、没有按钮调用这个新 ingress。

### 11.6 一条来自 Phase-2A 复核的补充（不弱化结构化硬拒绝）

```
GEOMETRY_SCAN_COVERS_EVERY_STRING_VALUE = True
GEOMETRY_SCAN_IS_FIELD_SCOPED_WHEN_THE_SPEC_CARRIES_PROSE = False
GEOMETRY_SCAN_SCOPE_DEFERRAL = 当 DiagramSpec 出现真正的工程备注/annotation 文本时，
  把锚点扫描按字段语义限定到"布局指令"字段，把散文当内容读；不得弱化结构化几何硬拒绝。
```

今天"扫所有字符串值"是**当前状态**，不是永久性质；将来收窄扫描范围时，替换动作是可见的。

### 11.7 Phase-2B Step 2 — deterministic partition / rank / absolute placement

链：

```
SemanticLayoutPlan → deterministic partition/rank → deterministic absolute node placement → placement projection
```

**只消费**：`primary_flow_direction`、`system_order`、`grouping`、`density`。
**仍不做**：routing、waypoints、避障、标注、`content_bounds`、`canvas_bounds`、最终 `canonical_layout_digest`。

#### density：离散类别 → 引擎规则，模型不携带数值

```
MODEL_MAY_DECLARE_GAP_NUMBERS            = False
DENSITY_SPACING_POLICY_IS_ENGINE_RULES   = True
DENSITY_POLICY_CHANGE_REQUIRES_THE_LAYOUT_RULES_VERSION_BUMP = True
SEPARATE_SPACING_POLICY_VERSION_ALLOWED_IN_V1 = False
UNKNOWN_DENSITY_IS_A_HARD_FAILURE        = True
```

模型只能说 `density = compact | comfortable`；**绝不能输出** `node_gap = 80` / `component_gap = 120`。
`DENSITY_SPACING_POLICY` 是引擎的机器数据（`compact` / `comfortable` → `rank_gap`、`node_gap`、
`system_gap`、`component_gap`），完全受 `layout_rules_version` 管辖：

```
改变 compact 对应的 node_gap  →  必须 bump layout_rules_version  →  layout digest 自然改变
```

因此 **v1 不引入独立的 `spacing_policy_version`**：一个规则版本源更好，digest 也不需要把每个
gap 数值新增成 `LAYOUT_DIGEST_INPUTS` 的独立字段。三条钉住：每个 density 枚举恰有一套 policy；
未知 density **hard fail**；policy 中任何数值变化属于 layout-rules semantic change。

#### grouping：意图是类别，实现是版本化引擎规则

```
GROUPING_IS_INTENT_ONLY = True
UNSUPPORTED_INTENT_IS_NEVER_SUBSTITUTED = True
UNSUPPORTED_INTENT_PRODUCES_ZERO_PLACEMENT = True
UNSUPPORTED_INTENT_CLASSES_MAY_STAY_IN_THE_VOCABULARY = True

UNSUPPORTED_LAYOUT_INTENT = (
  grouping=grouped_by_zone → code=zone_grouping_requires_zone_membership
    （原因：模型没有 zones / zone_membership，兑现不了）
)
SUPPORTED_LAYOUT_INTENT_CLASSES = (grouping=grouped_by_system, grouping=flat)
```

怎么划 rank、留多少 system gap、组内怎么排序、跨组 edge 怎么处理——全部是引擎规则。
模型**不得**输出具体 group bounds。

**兑现不了就是拒，不是找最像的。** `grouped_by_zone` 在词表里合法（协议认识这个意图），
但当前**不可执行**——认识的意图 ≠ 可以执行的意图。因此：

```
grouped_by_zone + 无 zone 语义 → UnsupportedLayoutIntent(code=zone_grouping_requires_zone_membership)
                              → zero placement
不可兑现的 grouping             → 绝不静默替换，也绝不显式替换
grouped_by_system / flat       → 既有确定性行为不变
```

把 `grouped_by_zone` 降级成 `grouped_by_system` 即使自报降级，仍然是**替换了调用方明确声明的
布局意图**——"看起来像答案"不等于"是对那个问题的答案"。将来 `DiagramSpec` 真正引入
zone membership 时，它才从 unsupported capability 转为 implemented capability。

引擎侧对应 `require_supported_intent()` / `resolve_grouping()`：不可兑现的意图在**计算任何东西之前**
被拒（`UnsupportedLayoutIntentError`，携带声明的 code），所以不会产生半张图。

当前引擎规则（`auto_layout_semantic.py`）：

| grouping | 排布 |
|---|---|
| `grouped_by_system` | 每个系统沿**横轴**占一条带，带间为 `system_gap` |
| `flat` | 单带，各系统沿**流向轴**首尾相接，接缝为 `component_gap` |
| `grouped_by_zone` | **不执行**：拒（见上），不产生 placement |

`SemanticLayoutPlan` 因此**不需要** `honoured_grouping` / `grouping_fallback_from` 这类字段：
不可兑现的意图在 plan 被放置之前就被拒了，所以 `intent.grouping` **永远就是**被兑现的那个类别。
（这两个字段随本次修复一并移除，plan digest 因此升到 `/3`。）

rank 规则（同样是引擎规则，两条是**选择**而非推论）：

1. **环被排序，不被拒绝，也不塌成一堆**：环内成员按 `engineering_id` 占连续 rank（`_ranks` 用
   SCC condensation 上的最长路，Tarjan 迭代实现，邻接表排序保证确定性）。
2. **只有系统内部的边影响该系统 rank**：跨系统连接是 Step 3 的走线问题；让它合并两条带的
   rank 结构，分区就不再意味着任何东西。

#### placement 不得影响 topology identity

```
PLACEMENT_NEVER_WRITES_INTO_THE_TOPOLOGY = True
SEMANTIC_DIGEST_IS_UNCHANGED_BY_PLACEMENT = True
PLACEMENT_PROJECTION_FIELDS = (placement_kind, engineering_id, x, y, width, height)
PLACEMENT_PROJECTION_VERSION = m7-placement-projection/1
PLACEMENT_PROJECTION_IS_A_PREFIX_OF_THE_CANONICAL_PROJECTION = True
PLACEMENT_KINDS = (equipment, instrument, annotation, routing)
STEP_2_PLACEMENT_KINDS = (equipment, instrument)
```

放置结果是**拓扑旁边的一个投影**，不是写回 node 的 x/y。于是下面这条等式是**机器可证的**：

```
semantic digest before Step 2 == semantic digest after Step 2
```

`placement projection` 是 `canonical layout projection` 的**前缀**（同一批字段名，但少掉 Step 3 的
`ordered_waypoints` 与 Step 4 的包络），所以它不会漂成第二套字段词汇。placement row 的字段集
**双向精确对拍**，排序键沿用 canonical 复合键 `(placement_kind, engineering_id)` 且必须唯一（全序）。

`PLACEMENT_INVARIANTS`（Step 2 的核心验收）：

```
每个需要 placement 的 node 恰有一个 placement
没有额外 engineering_id
没有缺失 engineering_id
placement 不修改 node / tag / system membership / edge
相同 topology + intent + rules → 相同 placement
没有两个被放置的 node 重叠
每个坐标有限且量化到声明的 coordinate quantum（1e-6）
```

节点尺寸在 routing 之前由引擎规则声明（`NODE_SIZE_POLICY`），并**显式记名**：

```
NODE_SIZE_IS_DECLARED_BY_ENGINE_RULES_UNTIL_ROUTING = True
```

真正的 symbol 尺寸属于 Step 3（第一个必须为走线留出空间的一步）。

#### 还有一个身份需要 bump

Step 2 给 `SemanticLayoutPlan` 增加了 `node_kinds` 与 `flow_edges`（几何自由的图事实）——
**v1 的字段集变了，所以 plan digest 版本升到 `m7-semantic-layout-plan-digest/2`**。
随后 grouped_by_zone 改为硬拒绝，`honoured_grouping` / `grouping_fallback_from` 两个字段被移除
（字段集再次变化）→ **`m7-semantic-layout-plan-digest/3`**。
字段集是版本语义的一部分：在旧版本下加/删字段，就是"不同的 plan 顶着同一个名字"，
这正是本里程碑要消灭的那种失败，只是低了一层。**未发布的中间版本也不豁免**——
版本号是字段集的函数，不是发布记录的函数。

#### 散文扫描延期的触发条件（机器规则，不挂 milestone）

```
GEOMETRY_SCAN_SCOPE_TRIGGER = the_diagram_specification_gains_a_prose_bearing_field
FIELD_SCOPED_SCAN_IS_MANDATORY_BEFORE_SUCH_A_SCHEMA_SHIPS = True
```

触发它的不是"走到了 M7 某阶段"，而是**`DiagramSpec` 第一次新增允许自由工程散文/annotation 的字段**。
届时：结构化几何键**仍然全局递归 hard reject**；相对锚点语言扫描**只作用于声明为
"可承载布局指令"的字段**，普通工程散文不扫描。**不提前实现，也不弱化已有结构化硬拒绝。**

## 12. M7-2 Phase-2B Step 3 — 冻结的 symbol geometry（几何物化、路由、标注）

Step 2 用的 Node 尺寸是**引擎规则**（`NODE_SIZE_POLICY`）。Step 3 第一次拿真实符号几何，
于是出现一个新的变化源：**目录本身**。同一个 topology、同一个 rules 版本，只因为某个符号的
包围几何改了，就**可以合法地**画出不同的图——所以「这张图是用哪份 symbol geometry 摆的」
必须成为布局身份的一部分。

落实的文件：契约 `m7_layout_contract.py`；运行时分三段却是同一权威：

```
agentcad/m7_symbol_geometry.py     冻结快照（事实）
agentcad/m7_endpoint_binding.py    端点端口绑定（explicit / inferred_unique）
agentcad/auto_layout_geometry.py   几何物化 + 确定性重排 + 正交路由 + 标注
```

三者都声明在 `PHASE_2B_MAY_IMPORT_THE_CONTRACT` 里（不在测试里），并作为 mixin 落在
**同一个** `AutoLayoutEngine` 上：`layout_semantic_topology`（step 1/2）→
`layout_semantic_geometry`（step 3）。引擎旁边另起一个"placer/router"就是第二个几何权威。

### 12.1 两侧的边界（谁是事实、谁是决定）

```
SymbolGeometryCatalogSnapshot 只提供事实/约束：
  symbol_key · renderer_geometry_identity · intrinsic_width/height ·
  scale_constraint · ports(port_id, direction, medium, normalized_x, normalized_y)

AutoLayoutEngine 仍然独占实例决定：
  instance width/height · x/y · rank · system_band · route · canvas
```

对应契约数据：

```
SYMBOL_CATALOG_OWNS                 = (symbol_key, renderer_geometry, intrinsic_bounds,
                                       scale_constraint, port_identities, port_anchors, port_directions)
LAYOUT_ENGINE_OWNS_FOR_SYMBOLS      = (instance_width, instance_height, instance_x, instance_y,
                                       rank, system_band, route, canvas)
SYMBOL_GEOMETRY_FACT_EXCLUSIONS     = (x, y, rank, system_band, route, waypoints, canvas,
                                       runtime_timings, document_revision)
SNAPSHOT_CARRIES_INSTANCE_GEOMETRY  = False
SNAPSHOT_CARRIES_LAYOUT_DECISIONS   = False
```

- 每个事实字段都在契约里**声明来源**（`SYMBOL_GEOMETRY_FACT_SOURCES`），校验器**双向**检查：
  声明的事实必须有来源，目录声称拥有的东西必须真的被读（否则就是一句注释，不是边界）。
- **端口锚点是归一化的**（占 intrinsic 尺寸的比例）：引擎可以把实例画得比目录标称尺寸大或小，
  绝对锚点只在目录自己的尺寸下才对。
- 目录**没有**「最小尺寸」这个概念，所以契约显式声明
  `SYMBOL_GEOMETRY_HAS_A_SEPARATE_MINIMUM_BOUND = False`——不去编一个最小值出来。
- 缩放约束来自目录 metadata（`scale_constraint`），默认 `scalable_preserving_aspect`；
  声明了**词表外**的值是硬拒绝（`unknown_symbol_scale_constraint`），不静默取默认。

### 12.2 closure 身份：digest 覆盖的是「这张图用到的符号」，不是整个目录

```
SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION   = m7-symbol-geometry-catalog-digest/1
SYMBOL_GEOMETRY_DIGEST_INPUT_IS_THE_LAYOUT_CLOSURE = True
SYMBOL_GEOMETRY_CLOSURE_IS_COMPLETE      = True
MISSING_SYMBOL_GEOMETRY_IS_A_HARD_FAILURE_BEFORE_ROUTING = True
MISSING_SYMBOL_GEOMETRY_FALLS_BACK_TO_RULE_SIZES         = False
```

- 一张图用了 17 个 symbol key → digest 只覆盖这 17 个。**新增一个无关符号定义不得改变**
  一张没动过的图的身份（否则身份噪声与真实变化无法区分）。
- 反方向必须完整：**每个需要 renderer geometry 的 node 都要有且只有一条已解析的几何记录**；
  目录里查不到的 key **在 routing 之前**硬失败，**不得退回 Step 2 的规则尺寸继续画**。
- 「事实相同 → digest 相同」；事实顺序不影响 digest（按 `symbol_key` 排序计算）。

### 12.3 身份级联：symbol geometry 进入最终 layout digest

```
LAYOUT_DIGEST_VERSION      = m7-layout-digest/2      （v1 → v2：新增输入）
LAYOUT_DIGEST_INPUTS      += symbol_geometry_catalog_digest
LAYOUT_PROJECTION_VERSION  = m7-layout-projection/1   （字段集未变 → 不动）
```

不把 symbol catalog 偷塞进 `layout_rules_version`：**符号定义改了**与**间距规则改了**是两个
独立变化源，合成一个版本号就再也答不出「这张图为什么变了」。
snapshot 自己的 digest 版本单独存在（`m7-symbol-geometry-catalog-digest/1`），
且**不得与 layout digest 共用版本串**。

### 12.4 node 的 symbol key 是**显式字段**（spec v2），不与工程类别混用

```
SYMBOL_KEY_FIELD                              = symbol_key          （必填，非空）
SYMBOL_KEY_IS_EXPLICIT                        = True
ENTITY_SYMBOL_KEY_FALLS_BACK_TO_THE_ENGINEERING_CLASS = False
SYMBOL_KEY_MUST_EQUAL_THE_ENGINEERING_CLASS   = False
SYMBOL_KEY_REQUIREMENTS = (exists_in_the_frozen_catalogue,
                           renderer_supported,
                           entity_kind_compatible)
```

分工（两个事实，今天碰巧相同，**不允许永久绑定**）：

```
equipment_class / instrument_type  = 工程语义：这个对象是什么
symbol_key                         = renderer/catalogue binding：用哪个标准符号表达它
```

将来同一个工程类别可以有多个合法图形变体，renderer 目录也可以重构而工程类别不变，
所以 `symbol_key` 必须显式。三个要求全部在**冻结快照**上检查（不是活 registry）：

- `exists_in_the_frozen_catalogue`；
- `renderer_supported`：目录没有声明任何 shapes 的符号"可列举不可绘制"，是硬拒绝；
- `entity_kind_compatible`：仪表节点只能用 `仪表` 类符号，设备节点不能用 `仪表`；
  把压力指示仪画成容器是**错误的图**，尽管两者都可渲染。

两个 digest 各自回答各自的问题：

```
engineering semantic digest   ← symbol_key 不变（换等价图形不改工程事实）
adapter/topology/plan identity ← symbol_key 必须在内（排版输入确实变了）
ENGINEERING_SEMANTIC_DIGEST_IS_UNCHANGED_BY_SYMBOL_KEY = True
```

级联升版（已批准）：`m7-diagram-spec/1→/2`、`m7-adapter-topology-digest/1→/2`、
`m7-semantic-layout-plan-digest/3→/4`。Phase-2A 的历史 commit **不重写**；
夹具的错引用以 **独立 erratum commit** 修在 Step 3 range 内（`pressure_transmitter →
pressure_indicator`、`purifier.tap → purifier.out`）。

### 12.5 Step 2 的 placement 是**初解**，不是终局

```
STEP_2_PLACEMENT_IS_THE_INITIAL_RANK_SOLUTION                 = True
PLACEMENT_MUST_BE_REVALIDATED_AGAINST_MATERIALIZED_GEOMETRY   = True
MATERIALIZED_GEOMETRY_MAY_TRIGGER_DETERMINISTIC_REFLOW        = True
REFLOW_IS_DETERMINISTIC                                       = True
REFLOW_MAY_CHANGE_ENGINEERING_SEMANTICS                       = False
```

真实尺寸进来后：materialize → 校验间距/无重叠；不满足则**确定性重排**。
保证：`same plan + same snapshot + same rules → same materialized placement`；
symbol geometry 变化 → 要么结果合法地相同，要么 **digest 不同**，
**绝不出现看不见的身份漂移**。

### 12.6 routing

```
ROUTING_RULES = (
  every semantic connection produces exactly one routed connection projection,
  routing may add waypoints and may not add or delete semantic connections,
  route endpoints are derived from the frozen symbol port geometry,
  every waypoint is finite and quantized to the declared coordinate quantum,
  the same plan, snapshot and rules produce the same route,
  a route never crosses the bounds of a node it does not serve,
)
UNROUTABLE_EDGE_IS_A_HARD_DIAGNOSTIC = True
UNROUTABLE_EDGE_FALLS_BACK_TO_A_STRAIGHT_LINE_THROUGH_EQUIPMENT = False
DEGRADED_ROUTING_POLICY_IS_DEFINED = False
```

T 不了的正交路由是**硬诊断**，不是"画条直线穿设备"的兜底：兜底路线以后要单独定义
degraded-routing policy 才算数。

端口绑定（routing 之前就解析成不可变结果，routing 只消费、不再猜第二次）：

```
ResolvedEndpointBinding(node_id, symbol_key, port_id, resolution = explicit | inferred_unique)

显式填写非空 port_id：
  必须命中冻结 symbol 的真实端口，且方向兼容
    不存在            → port_not_found
    方向不兼容        → port_direction_mismatch（不得自动换端口）

未填写：
  source → out / bidirectional 兼容端口；target → in / bidirectional 兼容端口
    候选 = 1  → 合法自动绑定，resolution = inferred_unique
    候选 = 0  → hard fail: no_compatible_port
    候选 > 1  → hard fail: ambiguous_port_binding（诊断列出候选）

SORTED_PORT_IDS_ARE_NOT_AN_INFERENCE_RULE = True   （"排序后取第一个"只是确定性的任意选择，不是工程推导）
CATALOGUE_DEFAULT_PORT_IS_NOT_INVENTED_IN_V1        = True
ROUTING_CONSUMES_RESOLVED_BINDINGS_ONLY             = True
ROUTING_MAY_RE_GUESS_A_PORT                         = False
```

### 12.7 标注：text extent 是纯代码，机器不进布局身份

```
ANNOTATION_TEXT_EXTENT_IS_DETERMINISTIC = True
ANNOTATION_TEXT_EXTENT_RULE_SOURCE      = agentcad.annotation_layout.text_bounds
ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR = 0.6
ANNOTATION_PLACEMENT_READS_BROWSER_FONT_METRICS = False
ANNOTATION_PLACEMENT_READS_OS_FONTS             = False
ANNOTATION_PLACEMENT_USES_CANVAS_MEASURE_TEXT   = False
```

现有规则（`width = max(font_size, len(text) * font_size * 0.6)`）是纯函数、无浏览器字体测量、
无 OS 字体依赖，全仓无 `measureText`/`getContext`，所以**复用**而不是新造一个 text-metrics
contract：算法一字不改，只把它归入 **layout rules 身份轴**：

```
ANNOTATION_TEXT_METRICS_POLICY = "deterministic_codepoint_extent_v1"
ANNOTATION_TEXT_METRICS_CHANGE_REQUIRES_LAYOUT_RULES_VERSION_BUMP = True
TEXT_METRICS_IS_NOT_A_SEPARATE_DIGEST_INPUT = True   （不新增顶层 text_metrics_digest）
```

身份轴保持两条：`text extent 算法 → layout_rules_version`；
`symbol renderer geometry → symbol_geometry_catalog_digest`。
黄金用例用**固定文本/字号/期望宽度**（`ANNOTATION_TEXT_METRICS_GOLDEN_CASES`），
**不 hash Python 源码/字节码**；其中必须包含一个"字号下限起决定作用"的短标签用例。
算法的确定性 ≠ 排版保真度：中文/复杂字符宽度将来可能要做质量升级，那是 annotation quality
问题，**不在 Step 3 顺手重写**。

### 12.8 Step 3 的身份版本与固定顺序（routing 开始前的两个前置条件）

```
m7-diagram-spec/2
m7-adapter-topology-digest/2
m7-semantic-layout-plan-digest/4
m7-symbol-geometry-catalog-digest/1
m7-layout-digest/2
m7-layout-projection/1            （未变：canonical projection 字段集没有变）
```

```
DiagramSpec v2 + explicit symbol_key
  → adapter / topology / plan identity 升版
  → used-symbol geometry closure snapshot
  → endpoint binding resolution
  → actual geometry materialization
  → deterministic reflow if needed
  → non-overlap verification
  → orthogonal routing
  → obstacle avoidance
  → deterministic annotation placement
```

routing 开始前**必须**已经满足（否则 hard fail，不得退回 Step 2 的假定尺寸 / 猜测端口继续画）：

```
每个 renderable node      → exactly one symbol geometry fact
每个 connection endpoint  → exactly one resolved port binding
```

### 12.9 「valid」在两个时刻是两个问题（实测出来的差别）

Step 2 按 **origin 间距** = density policy 摆位（已签署的事实）；Step 3 问的是**真实尺寸下会不会重叠**。
两者不是同一个量：拿 Step 2 的坐标去要求"清晰间距 ≥ policy"，**每一张图都会被判为坏图**
（origin 间距 220 减掉 120 宽 = 100 清晰间距）。所以契约把两个时刻分开记名：

```
MATERIALIZED_PLACEMENT_MUST_NOT_OVERLAP                = True   （step 2 继承的坐标：只要求不重叠）
STEP_2_ORIGIN_SPACING_IS_INHERITED_NOT_RECHECKED       = True
REFLOWED_PLACEMENT_MUST_SATISFY_THE_MATERIALIZED_CLEARANCE_POLICY = True （重排后的行：按下述净空策略校验）
REFLOW_VERIFICATION_USES_THE_MATERIALIZED_CLEARANCE_POLICY        = True
```

结果：fixture A 的真实尺寸恰好放得下 → **保留 Step 2 的坐标**（`placement_reflowed = False`），
`reflow_payload`（同 rank 两个 90×140 储罐）放不下 → **确定性重排**（`placement_reflowed = True`）。
两条路径都有测试，不是死分支。

### 12.10 routing 的"可以穿自己"与"优选不穿"

```
ROUTE_MAY_CROSS_THE_NODE_IT_SERVES = True                        （端口在符号内部时只能如此）
ROUTE_PREFERS_A_SHAPE_THAT_AVOIDS_EVEN_THE_NODES_IT_SERVES = True（更体面的画法优先，非强制）
```

候选形状固定为 4 种（两种单拐弯 + 两种走廊中段），按声明顺序取第一个通过碰撞检测的；
两遍：先要求**避开所有节点**，都不行再退回"只避开不服务的节点"。两遍都是确定的；
全都不通过 → `UnroutableEdgeError`（`no_orthogonal_route`），绝不画穿设备的直线。

路由用到的 stub 长度、实例缩放、标注间距都是**声明的引擎规则**（`ROUTE_STUB_LENGTH`、
`INSTANCE_SCALE_FACTOR = 1.0`、`ANNOTATION_LABEL_GAP` / `ANNOTATION_CLEARANCE` /
`ANNOTATION_PLACEMENT_ATTEMPTS`），改任何一个都是 rules 版本变更；`INSTANCE_SCALE_FACTOR`
v1 取 1.0（用目录标称尺寸），因为"实例缩放"是有排版后果的真特性，现在编一个系数等于把
一个没人选过的数字写进每一张图。

## 13. M7-2 Phase-2B Step 4 —— content bounds 与派生画布（画布是链条的尾巴）

Step 4 回答的问题是"这张图需要多大地方"。**答案是一次测量，不是一条指令**——顺序本身就是结论：
把画布交给引擎等于把第二个几何权威交出去，而 `1600×900` 那个默认值正是"没人选过、所有人继承"
的坐标权威。本步落地 `auto_layout_canvas.py`，仍然作为 mixin 落在那**一个** `AutoLayoutEngine` 上
（`layout_semantic_canvas`）：在引擎旁边算画布，就是对同一张图的第二种意见。

### 13.1 content bounds 覆盖什么：一个闭集

```
CONTENT_BOUNDS_INPUTS = (symbol_instance_geometry, orthogonal_routes, annotations_and_leader_lines)
CONTENT_BOUNDS_IS_A_CLOSED_LIST = True
CONTENT_BOUNDS_MUST_COVER_EVERY_PRESENTATION_ROW = True
CONTENT_BOUNDS_MAY_EXCLUDE_A_PRESENTATION_ROW = False
ROUTE_STROKE_WIDTH_CONTRIBUTES_TO_CONTENT_BOUNDS = False   （管路贡献路径点，不贡献线宽）
ANNOTATION_EXTENT_IS_THE_TEXT_BOX = True                   （标签的盒子就是 §12 那条纯文本规则）
```

"画布恰好装下这张图"这句话只有在"这张图"被枚举之后才可检验：**由子集算出的 bounds 就是一张会裁图的画布**，
而子集正是这件事藏身的地方。

### 13.2 margin：声明的引擎规则，按 density 档位

```
CANVAS_MARGIN_POLICY_IS_ENGINE_RULES = True
CANVAS_MARGIN_IS_KEYED_BY_THE_DENSITY_CLASS = True
UNKNOWN_CANVAS_MARGIN_CLASS_IS_A_HARD_FAILURE = True
CANVAS_MARGIN_DEFAULT_IS_APPLIED_IN_SILENCE = False
MARGIN_IS_PRESERVED_IN_THE_DERIVED_CANVAS = True
```

引擎侧 `CANVAS_MARGIN_POLICY = (compact 32, comfortable 48)`，与 spacing policy 同一形状、同一理由：
没人声明的 margin 会随网格、宿主或导出格式漂移，于是同一张图两次跑出两个画布。比例强制**不得挪用** margin
（可以再添空，不能拿 margin 去买比例）。

### 13.3 aspect class 是比例；只增不减；orientation 定方向

```
ASPECT_CLASS_TARGET_RATIOS = (standard 4/3, wide 16/9, extra_wide 12.0)
ASPECT_CLASS_IS_A_RATIO_NOT_A_SIZE = True
ASPECT_ENFORCEMENT_ONLY_GROWS_THE_CANVAS = True
ASPECT_ENFORCEMENT_MAY_SHRINK_BELOW_THE_CONTENT = False
CANVAS_NEVER_CROPS_CONTENT_TO_REACH_A_RATIO = True
ORIENTATION_IS_ENFORCED_ON_THE_DERIVED_CANVAS = True
ORIENTATION_APPLIES_TO_THE_ENFORCED_RATIO_DIRECTION = True
```

`landscape` = `width/height` 是该档比例，`portrait` = `height/width` 是该档比例；缺的那一轴被**拉长**，
两条边取整都朝**外**（`_floor_to_quantum` / `_ceil_to_quantum`），所以"量化后的画布仍然装得下内容"
是推导的性质，而不是希望。实测 fixture A：content `374×320` → canvas `4608×384`（12:1）。

### 13.4 无裁剪是**校验**出来的，不是论证出来的

```
CANVAS_CLIPS_NOTHING_IS_VERIFIED_NOT_ASSUMED = True
PRESENTATION_GEOMETRY_OUTSIDE_THE_CANVAS_IS_A_HARD_FAILURE = True
CANVAS_VERIFICATION_COVERS_ROUTES_AND_ANNOTATIONS = True
```

链条论证（"画布是从内容派生的"）与性质（"没有任何东西伸出去"）是两句不同的话，只有第二句在说这张图。
这里有一个**实测出来的形状差异**，值得记住：`extra_wide` 下横向几乎不可能裁剪——比例把画布拉到内容高度的 12 倍，
所以横向多出几个 margin 仍然装得下；**紧的那一轴是纵向**（横向拉长后 `height` 恰好等于 content + 2×margin）。
因此"子集 bounds 会裁图"的变异必须在纵向取景，否则它会看着无害。

### 13.5 端点 escape 段：Step 3 裁定的收窄，落在最终几何校验里

```
ROUTE_ENDPOINT_ESCAPE_SEGMENT_MAY_INTERSECT_ITS_ENDPOINT_NODE = True
ROUTE_SEGMENTS_BEYOND_THE_ESCAPE_SEGMENT_MUST_STAY_OUTSIDE_PROTECTED_NODE_INTERIORS = True
ROUTE_NODE_INTERSECTION_IS_VERIFIED_IN_FINAL_GEOMETRY_VALIDATION = True
ROUTE_MAY_CROSS_ANY_NODE_IT_SERVES = False
```

§12 允许"穿过自己服务的节点"（端口锚点在符号内部时只能如此），但语义必须窄：**每个端点恰好一段**
（离开源锚点的第一段、进入目标锚点的最后一段），其余所有段——包括对自己端点的其余段——都必须在节点内部之外。
这条校验放在最终几何校验里，而不是路由器的候选排序里：在候选排序里，路由器同时是法官和被告。

### 13.6 重排的规则有自己的名字：materialized clearance policy

远端裁定：Step 2 保持已签署的 `origin spacing` 语义，不追改为 clear-gap；但 Step 3 那个"declared clear separation"
必须**明确命名成另一条引擎规则**，别让人读成 `rank_gap`：

```
MATERIALIZED_NODE_CLEARANCE_POLICY_NAME = "materialized_clearance_policy_v1"
MATERIALIZED_CLEARANCE_FIELDS = (minimum_node_clearance, minimum_system_clearance)
MATERIALIZED_CLEARANCE_IS_NOT_THE_RANK_GAP = True
MATERIALIZED_CLEARANCE_POLICY_IS_UNDER_THE_LAYOUT_RULES_VERSION = True
SEPARATE_CLEARANCE_POLICY_VERSION_ALLOWED_IN_V1 = False
UNKNOWN_CLEARANCE_CLASS_IS_A_HARD_FAILURE = True
```

`rank_gap` 是 lattice 上的**原点间距**，净空是**已物化符号边界之间**的余量；120 宽的节点放在 150 的原点间距上
净空 30，它两条规则都没违反。校验器因此还双向检查：净空字段**不得**与 spacing policy 的字段重名
（`DENSITY_SPACING_POLICY_FIELDS`），否则一条规则的签核会变成另一条的无声签核。

### 13.7 意图闸门与交付

Step 4 是最后两个意图维度（`orientation`、`preferred_aspect_class`）的**施加点**，所以过了这一步
`plan.pending_intent_dimensions == ()`——六个维度全部有落点，这正是 §11 里"声明了却不生效"那条 Gate 的兑现。
plan 字段集增加 `content_bounds`，因此 `m7-semantic-layout-plan-digest` `/5 → /6`（字段集变就是版本变，
未发布的中间版本不豁免）。门禁：ruff clean、`validate_contract()` 空、pytest 1351 passed。

### 13.8 stroke 也是 presentation geometry（Gate 抓到的窄 blocker）

原 §13 同时声明了"所有 presentation geometry ⊆ canvas_bounds"和"route 的 stroke 不计入 bounds"，
这两条不能同时成立：管路/引线/符号轮廓都是有宽度的路径，**渲染范围在中心线两侧**。
更糟的是"margin=32 通常够把 stroke 包进去"只是**两个声明数字碰巧的大小关系**，不是 bounds 对实际呈现的证明。
而且这条假设连符号都没放过：目录里 `gas_tank` 的 port stub 画到 `x = 0`（`M`/`line` 坐标直接落在盒边上），
所以 1.5px 的轮廓线本来就会画到声明盒外面 0.75px。

修正后的形状（**每个 kind 的 stroke 都归 presentation bounds**）：

```
PRESENTATION_BOUNDS_ARE_RENDERED_EXTENTS = True
CONTENT_BOUNDS_INPUTS_ARE_RENDERED_EXTENTS = True
CONTENT_BOUNDS_MAY_USE_A_CENTERLINE_INSTEAD_OF_A_RENDERED_EXTENT = False
ROUTE_STROKE_CONTRIBUTES_TO_PRESENTATION_BOUNDS = True
LEADER_STROKE_CONTRIBUTES_TO_PRESENTATION_BOUNDS = True
SYMBOL_OUTLINE_STROKE_CONTRIBUTES_TO_PRESENTATION_BOUNDS = True
ANNOTATION_TEXT_HAS_NO_STROKE = True
PRESENTATION_STROKE_POLICY_KINDS = (symbol_outline, connector, leader_line, annotation_text)
PRESENTATION_STROKE_ENVELOPE_RULE =
    "geometry_inflated_by_half_stroke_plus_declared_cap_join_allowance_v1"
PRESENTATION_STROKE_POLICY_IS_UNDER_THE_LAYOUT_RULES_VERSION = True
PRESENTATION_STROKE_POLICY_CHANGE_REQUIRES_LAYOUT_RULES_VERSION_BUMP = True
PRESENTATION_STROKE_IS_NOT_A_SEPARATE_DIGEST_INPUT = True
SYMBOL_BOX_IS_NOT_THE_SYMBOL_RENDERED_EXTENT = True
LEADER_LINE_ROWS_EXIST_IN_THE_PLAN = False        （policy 覆盖引线，但当前还没有引线行）
```

引擎侧一条策略表，reach = `stroke_width / 2 + cap_join_allowance`：

```
symbol_outline  stroke 1.5  allowance 0.75  → reach 1.5
connector       stroke 1.5  allowance 1.0   → reach 1.75
leader_line     stroke 1.0  allowance 0.5   → reach 1.0
annotation_text stroke 0    allowance 0     → reach 0   （文本框本身就是它的范围）
```

渲染范围 = 几何 **inflate** reach（对轴对齐折线，逐点方盒膨胀即折线的 Minkowski 包络）；
`clipping_problems()` 检查的是**膨胀后**的 bounds（node / route waypoint / annotation 各报名字）。
阈值都归 `layout_rules_version`，不新增 `stroke_digest`。

**两个 fixture + mutation**：

1. 需要一张"中心线全在里面、stroke 越界"的画布。注意它**不可能**来自本步的派生——margin(32) 远大于
   reach(1.75)，派生出来的画布永远不会裁自己的 stroke。这正是这个校验存在的理由：它面向的是**来自别处**的画布
   （旧默认、调用方、更小的包络）。fixture 因此取"中心线包络外扩 1 个单位"，使中心线判定与渲染判定给出
   不同答案；测试同时断言"每个中心线都在里面"（旧的判定会全绿）与"确实有 stroke 越界"。
2. 引线同类：`presentation_stroke_envelope("leader_line").reach > 0` 且 `rendered_extent` 对引线中心线生效。
   当前 plan 不产生引线行，这一点作为**声明的事实**（`LEADER_LINE_ROWS_EXIST_IN_THE_PLAN = False`）写下来，
   免得"策略覆盖引线"被读成"已经有引线要覆盖"。
3. mutation：**去掉 stroke 膨胀 → 上述 fixture 必须红**（实测 2 条红）；只去掉符号轮廓膨胀也红（1 条红）。
   我第一版的符号断言里带了一个 `or` 逃生口，mutation 全绿——说明它是假守卫，已改成经
   `presentation_boxes` 读、并逐条断言 `box == nominal.expanded(reach)`。

**版本结论**：不升 `LAYOUT_RULES_VERSION`。旧声明（"route stroke 不计入 bounds"）只存在于 `ad00e2c`
——**未签名、未 push、没有任何一张图是用它量过的**——所以这属于"把尚未发布的定义补完整"，而不是改动一条
已生效的规则。反过来说，前向规则已经命名为 `PRESENTATION_STROKE_POLICY_CHANGE_REQUIRES_LAYOUT_RULES_VERSION_BUMP`，
以后改 stroke 宽度或 envelope 规则就是 rules 版本变更，不能再走这条捷径。
`clipping_problems()` 的检查顺序也调整成**先裁剪、后 margin**：裁剪是关于这张图的属性（对任何画布都成立），
margin 是关于派生的属性；先报前者，读者看到的才是自己能看见的东西。
