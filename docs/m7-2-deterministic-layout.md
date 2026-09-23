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
GROUPING_FALLBACKS = ((grouped_by_zone → grouped_by_system, 原因：模型尚未表达 zone),)
```

怎么划 rank、留多少 system gap、组内怎么排序、跨组 edge 怎么处理——全部是引擎规则。
模型**不得**输出具体 group bounds。

当前引擎规则（`auto_layout_semantic.py`）：

| grouping | 排布 |
|---|---|
| `grouped_by_system` / `grouped_by_zone`（fallback） | 每个系统沿**横轴**占一条带，带间为 `system_gap` |
| `flat` | 单带，各系统沿**流向轴**首尾相接，接缝为 `component_gap` |

fallback **必须自报**：`plan.honoured_grouping` / `plan.grouping_fallback_from` 记录"实际兑现的是
哪个类别、替换掉了哪个类别"，所以"我们做了别的事"是可见的，而不是只能从图里反推。

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
**v1 的字段集变了，所以 plan digest 版本必须升到 `m7-semantic-layout-plan-digest/2`**。
字段集是版本语义的一部分：在 v1 下加字段，就是"不同的 plan 顶着同一个名字"，
这正是本里程碑要消灭的那种失败，只是低了一层。

#### 散文扫描延期的触发条件（机器规则，不挂 milestone）

```
GEOMETRY_SCAN_SCOPE_TRIGGER = the_diagram_specification_gains_a_prose_bearing_field
FIELD_SCOPED_SCAN_IS_MANDATORY_BEFORE_SUCH_A_SCHEMA_SHIPS = True
```

触发它的不是"走到了 M7 某阶段"，而是**`DiagramSpec` 第一次新增允许自由工程散文/annotation 的字段**。
届时：结构化几何键**仍然全局递归 hard reject**；相对锚点语言扫描**只作用于声明为
"可承载布局指令"的字段**，普通工程散文不扫描。**不提前实现，也不弱化已有结构化硬拒绝。**
