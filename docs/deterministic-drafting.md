# 确定性整理引擎（M3 Deterministic Drafting Engine）

本文件描述 Charter `§15 / §34（Priority 3）/ §48（M3）` 的落地：**layout、routing、
collision、annotation、regional repair、quality gate 达到稳定可重复结果**。

一句话定位：**它不是一个“让图变好看”的启发式，而是一条可复现、可回滚、可解释、只读的
确定性整理流水线**——它产出一份事务，由人（或受治理的 Agent 通道）决定是否落地。

## 1. 为什么需要它

项目里已经有三个“会动几何”的组件：

| 组件 | 职责 | 现状 |
|---|---|---|
| `auto_layout.py` | 拓扑感知排布（rank / 分层 / 正交走线） | 已有，本次接入“锁定集”作为锚点 |
| `annotation_layout.py` | 标签摆放与打分 | 已有，被整理流水线复用 |
| `diagram_quality.py` | 图面规则（斜线 / 微小线段 / 穿设备 / 文字重叠 / 评分） | 已有，是整理引擎的**评分与错误码来源** |

单独跑其中一个都会留下缺口：排布改了位置但管线不跟随；管线绕开了但标签压在符号上；标签
挪开了但走线穿过图例。**M3 增加的是把它们串成一条带统一判据（monotone）与统一门禁（gate）
的确定性流水线**，并且明确“谁都不许改拓扑”。

## 2. 引擎契约（七条）

这七条既是实现约束，也是测试与文档的对照表。

1. **只读（preview-only）**：引擎永不写文档。`preview` 返回一份
   `TransactionRequest`，落地必须走唯一受治理写通道（`POST /documents/{id}/transactions`
   或 Harness 的 `apply_deterministic_drafting`），因此权限、revision 校验、审计、undo
   一个都不少（Charter §7 / P0-2）。
2. **可复现（reproducible）**：所有 pass 在 id 规范序快照上运行，操作按规范序输出；结果
   带 `input_content_hash` / `output_content_hash` / `transaction_digest`。**同样的图纸内容
   永远得到同一个摘要**，与元素存储顺序、运行次数无关。
3. **不改工程连接（connectivity-preserving）**：只产出 `UpdateElementOperation`，且
   patch 永不包含 `source` / `target`；一旦出现即报 `DRAFT_TOPOLOGY_CHANGED` blocker。
4. **锁定优先（lock-honouring）**：锁定元素不被移动、不被重路由、不被改标签；它反而成为
   其余几何的锚点与障碍物。
5. **范围受限（scope-confined）**：`region` / `element_ids` 之外的元素不得改动，越界即报
   `DRAFT_OUT_OF_SCOPE_CHANGE` blocker。
6. **单调（monotone）**：每个阶段只有在**不使任何硬性图面指标变差**时才被接受，否则整阶段
   回滚并记 `DRAFT_STAGE_ROLLED_BACK`；结果里仍有回归则记 `DRAFT_RESULT_REGRESSION`。
   整理可以收拾图面，不能损坏图面。
7. **如实（honest）**：修不了的就报出来（悬空连接点、落在连接点上的跨线、锁在图例里的
   设备、无法绑定的端口），绝不猜、绝不删除、绝不静默美化分数。

## 3. 流水线

`DraftingEngine.preview_document` 按固定顺序执行，并重复到不动点（`pipeline_rounds`，默认
3 轮，有限且可复现）：

```text
锁定解析 → 区域排布(仅第 1 轮) → 端口感知重路由 → 标签摆放
        → 跨线桥接 → 保留空间驱逐 → 碰撞松弛        （循环到不动点）
```

| 阶段 | 做什么 | 明确不做什么 |
|---|---|---|
| 锁定解析 | 三种来源合成一个冻结集（见 §4） | 不写文档、不改元数据 |
| 区域排布 | 让已有的拓扑排布引擎在这个冻结集下工作 | 只在第 1 轮；`relayout=False` 时完全跳过 |
| 端口感知重路由 | 按端口外法线 / 障碍物 / 跨线约束重新生成正交路径 | 不重绑端口；锁定的管线不动 |
| 标签摆放 | 仅当某标签**自身**代价严格下降时才移动 | 不改文字内容，不动符号自带标签（那属于图面规则） |
| 跨线桥接 | 每条合法跨线恰好在**次要线**上打一个桥 | 落在连接点上的跨线只报告（`DRAFT_CROSSING_ON_JUNCTION`） |
| 保留空间驱逐 | 把未被锁定的符号/连接节点移出声明保留区 | 锁定的不动，改为 blocker |
| 碰撞松弛 | 一次只分离一对重叠节点，逐次接受 | 不创建/删除拓扑元素 |

为什么需要“重复到不动点”：这些 pass 天然互相影响——碰撞松弛挪动了一个节点，就让刚算好的
最优路径失效。**`settled` 是结果的属性，不是期望**：把上一次结果再喂回引擎，若 `transaction`
为 `None` 即表示已收敛。

## 4. 手动锁定（manual lock）

锁定是**图纸数据**，不是某次调用的参数。三种来源会被合成一个集合，并逐条记录来源：

| 来源 | 载体 | 语义 |
|---|---|---|
| `request.locked_element_ids` | 本次请求 | 一次性的临时冻结 |
| `element.metadata["drafting_lock"]` | 元素元数据 | 工程师在编辑器里钉住的对象，跨会话、跨调用方 |
| `metadata.layout_regions` 中 `kind="lock"` 的区域 | 图纸元数据 | “这块已确认，请绕着它排” |

`DraftingLocks` 会分别回报 `request_element_ids` / `metadata_element_ids` /
`region_element_ids` 及区域标签，`skipped_locked_element_ids` 记录“想动但因锁定而跳过”的
对象。前端的“锁定/解锁”就是一次**普通的受治理事务**（写元数据），因此它同样有 revision
校验、审计记录与 undo。

同一条锁定语义也接进了 `auto_layout`（`_locked_element_ids`）：排布引擎与整理引擎不会对
“什么算锁定”有两种理解。

## 5. 端口、走线与跨线/连接点语义

**端口**：`resolve_ports()` 输出每个可寻址端口的 `direction` / `side` / `outward_normal` /
`medium` / 当前负载（连接的元素与管线）。端口的绝对坐标只有一份实现
（`drafting_geometry.symbol_port_point`），`DocumentService._symbol_port_point` 直接委托到
它——**算出来的点和绑定的点不可能不一致**。绑定到不存在端口的管线不是被静默跳过，而是
`DRAFT_PORT_UNRESOLVED`。

**走线评分**是一个字典序元组，最重的放最前：

```text
(端口契约违背, 障碍物命中, 正交性/微小线段/折点数, 绕行长度)
```

因此引擎不会为了少一个折点而让管线穿过设备。重路由分两类：

* **forced**：本轮的移动改变了符号位置，挂在上面的管线**必须**跟上（与 `reroute_connectors`
  开关无关——“不要顺路整理”不等于“把你刚挪走的管子留在原地”）；
* **opportunistic**：该管线自己的走线质量严格变好时才重排。

**跨线 vs 连接点**：几何相交但工程上不相连的是跨线，需要跨线桥；`junction` 元素是显式
拓扑。桥只打一次、只打次要线（优先级：声明流向优先 → 折点少 → 更短），双桥会被收敛为单桥；
与锁定/范围外管线相关的跨线只报告（`DRAFT_CROSSING_UNBRIDGED_LOCKED`）。悬空连接点报
`DRAFT_JUNCTION_DANGLING`，**引擎永不删除或补接拓扑**（Charter P0-4：连接性是语义，不是样式）。

## 6. 保留空间（legend / 标题栏 / 禁布区）

声明在 `metadata.layout_regions`（`kind` 非 `lock`）的区域是**图纸数据**，因此：

* 走线把它们当障碍物参与评分（不会“先穿过去，再抱怨”）；
* 标签优先不落在里面；
* **未被锁定**的设备/连接节点若已经在里面，会被确定性地移出（`reserved_eviction` 阶段）；
* 被锁定的侵入者留在原处，并在门禁里如实成为 blocker
  （`DRAFT_RESERVED_REGION_OVERLAP`）——这是“工程师的决定 vs 系统的建议”的正确优先级。

## 7. 质量门禁（quality gate）

`DraftingGate.passed` 需要同时满足三件事：

1. 没有 drafting blocker（`DRAFTING_BLOCKER_CODES`，如越界变更、拓扑变更、未桥接跨线、
   保留区重叠、结果回归）；
2. 没有图面规则 error（来自 `diagram_quality` 的 issue，如穿设备、文字重叠、微小线段）；
3. 评分 ≥ `policy.target_score`（默认 95）。

门禁同时回报 `checked_codes`（它到底检查了哪些码）、`blockers`、`drawing_issues` 与
`waived_codes`。**豁免（waiver）不是删除**：被豁免的 finding 仍然出现在报告里，只是不阻断
门禁——项目可以接受某个既有缺陷，但必须看得见它。

CLI 与 CI 直接复用这个判定：`pid-agent drafting report|preview ...` 在门禁失败时退出码 **2**。

## 8. 接口面

| 面 | 入口 | 说明 |
|---|---|---|
| REST | `POST /api/v2/documents/{id}/drafting/report` | 只读测量：端口、跨线、连接点、碰撞、保留区、锁定来源、门禁 |
| REST | `POST /api/v2/documents/{id}/drafting/preview` | 只读：返回可复现事务 + before/after 指标 + 未被修复的 finding + 摘要 |
| MCP | `get_drafting_report` / `preview_deterministic_drafting` | 同上（只读） |
| MCP | `apply_deterministic_drafting` | 走 Harness 的 allow 策略、approval 与审计后落地 |
| CLI | `pid-agent drafting report\|preview <document_id>` | `--region` / `--element` / `--lock` / `--no-relayout` / `--target-score` / `--waive` / `--summary` / `--output`；门禁失败退出码 2 |
| Registry | `tool_registry.py` 三个 ToolDefinition | 与 `surface_contract.py` 的 HTTP/MCP 绑定同步登记，未登记即测试失败 |
| 前端 | 右侧「整理」tab（`DraftingPanel.tsx` + `drafting.ts`） | 分析/预览/落地/丢弃/锁定/解锁，展示门禁解释、指标 diff、findings 与摘要 |

两个 REST 路由都登记为 **read**：整理引擎没有私有写路径。这也意味着它能被安全地放进
只读环境（例如 CI 门禁）而不需要任何写权限。

## 9. 可复现性的具体含义

* `canonical_document()` 在任何 pass 之前按 id 排序元素；digest 只对规范投影计算，样式字段
  不参与。
* 操作按 `canonical_operations()` 输出，因此事务的字节级内容与发现顺序无关。
* `DRAFTING_ENGINE_VERSION` 在算法语义变化时必须提升；`transaction_digest` 把引擎版本、
  请求、输入/输出内容哈希与操作一起摘要，因此“同一摘要”= 同一算法 + 同一输入 + 同一结果。
* 幂等性有测试：把引擎的输出再喂回引擎，`settled=True` 且 `transaction=None`。

## 10. 明确边界（不在 M3 内）

* **不修语义缺陷**：重复位号、错误位号、缺端口的管线可以被**报告**，但引擎不改标签文字、
  不改编号——那属于工程内容，不是排版。
* **不造拓扑**：不新增/删除设备、管线、连接点，不做“自动补一条支路”。
* **不做工程规则校验**：ISA 位号结构、SIS 联锁、cause-and-effect、回路正确性属于
  Charter M4（Engineering Validation System）与 Priority 2 Validator Framework，本引擎只
  消费 `diagram_quality` 的图面规则。
* **不代替人做决定**：锁定元素的侵入、无法绕开的保留区、范围外的跨线都以 finding 形式
  交回给人。
* **不产生声明式稳定标识**：`identity_basis="element"` 的老图纸仍然只有图元级标识；让
  创建/导入层写出 `equipment_id` / `line_id` 等声明标识是**后续层的职责**（M2 已把机制备好，
  边界如实标注，见 `docs/engineering-semantic-graph.md`）。

## 11. 离线验收与回归

* `backend/tests/test_drafting_geometry.py`：几何/端口/跨线/连接点/保留区/摘要等纯函数契约。
* `backend/tests/test_drafting_engine.py`（27 例）：可复现、幂等、只读、拓扑不变、锁定三来源、
  区域/选择范围、route-only 模式、跨线单桥、悬空连接点、保留区驱逐（**未锁定被移出 / 锁定
  如实上报**）、门禁与豁免、硬性指标单调。
* `backend/tests/test_drafting_api.py` / `test_drafting_cli.py`：REST 与 CLI 面（含退出码）。
* 离线质量 Harness 新增 `deterministic_drafting_contract`：在一张**故意脏**的图上验证
  “可复现 + 只读 + 锁定优先 + 单调 + 拓扑不变 + 绕开图例并驱逐侵入者 + 再次运行即收敛”，
  并要求残留项只有引擎**不允许**修的语义缺陷（重复标签），门禁如实拒绝签字。
* 前端 `frontend/tests/drafting.test.ts`（纯函数）与 `frontend/e2e/drafting.spec.ts`（浏览器：
  预览不写、落地推进 revision、锁定是普通事务、API 只读且可复现、过期 revision 409）。

跑一次完整验收：

```bash
source .venv/bin/activate
ruff check backend
python -m pytest backend/tests -q
pid-agent quality-harness
cd frontend && npm test && npm run build && npm run test:e2e
```
