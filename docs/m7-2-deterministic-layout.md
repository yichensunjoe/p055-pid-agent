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
LAYOUT_DIGEST_INPUTS = (diagram_spec_semantic_digest, layout_engine_version,
                        layout_rules_version, canonical_placement_projection)
LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING = (created_at, duration_ms, iteration_count,
                                               provider, model, session_id, plan_id,
                                               attempt, layout_run_id)
CANONICAL_PROJECTION_IS_SORTED = True
CANONICAL_PROJECTION_SORT_KEY = engineering_id
CANONICAL_PROJECTION_IS_TOTAL_ORDERED = True
CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE = True
```

> 同一份 DiagramSpec + 同一 layout engine version + 同一 layout rules version
> → 同一个 canonical layout digest

摘要**逐字段**定义，因为"canonical"否则只是一个词：`CANONICAL_LAYOUT_PROJECTION_FIELDS`
把每个字段和它是否进入摘要都写下来。进入摘要的：

```
engineering_id（稳定工程身份，改名不改摘要）
placement_kind（设备/标注/走线的放置类别，防止"碰巧相等"）
x · y · width · height
ordered_waypoints（按遍历顺序，才可能比较两条走线）
content_bounds · canvas_bounds
```

不进入摘要的（易变记账）：

```
duration_ms · created_at · layout_run_id
```

把 `duration_ms` 这类字段放进摘要，会让**正确的重放看起来像一次改动**——
这和 A5 语料身份里"把运行期 volatile 混进身份"是同一个错。

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
