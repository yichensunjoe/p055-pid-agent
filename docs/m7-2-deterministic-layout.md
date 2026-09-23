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
