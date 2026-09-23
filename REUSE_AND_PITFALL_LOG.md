## 2026-09-23 · 「唯一输入」要看签名，不看自己写的文档；「写后校验」不是写前边界（P055-PID-Agent）

- 场景：M7-2 Phase-3 materializer，我在文档里写「唯一输入是 finalized canonical layout」，但实际签名是
  `materialize_canonical_layout(plan, *, document_id, labels: Mapping[str,str] | None = None)`。`document_id` 是落点无妨，
  `labels` 却让**同一个 finalized plan、同一个 canonical_layout_digest** 产出两张不同的图——
  「finalized layout → drawing」不再是函数关系。而且回读比对字段里没有 `text`/`label`，
  「几何/tag/connector 全对但图上文字错了」能 PASS。
- 结论做法：① **把参数删掉**，而不是加校验——文本从已被上游身份闸绑定的 `tag` 推导，
  再用**同一个纯函数**（`text_bounds`）证明「布局放的盒子就是该文本量出来的盒子」；不另写宽度公式（两套实现就是漂移的开始）。
  ② 回读比对加入 `text`/`label`；第二文本面（`SymbolElement.label`）写空但**读**它，不许假设它空着。
  ③ `add`-only + 写后回读有个明确失败模式：目标里已有无关元素 X → 整张图 add → **提交、revision+1、audit 写下** → 回读才发现多一行。
  所以基线必须在**写之前**冻结：`require_empty_target()` ＋ 把 preflight 读到的 revision 写进 `expected_revision`（用已有的原子机制）。
  ④ 审计链里的 `resulting_revision` **不许由调用方预测**——两个写者能做同一个预测；改成从 `result.document.revision` 读。
- 踩坑点：① **签名才是边界**。「唯一输入」这种话要拿 `inspect.signature()` 验；有校验的参数仍然是调用方在决定图的内容。
  ② 「空」的定义要写清楚是「没有**工程内容**」还是「没有**行**」——新建文档本来就带默认图层与 `system_default`，
  按「systems == []」写会把每个新建文档都拒掉。③ 测试要断言副作用没发生（revision 没动、元素没进去、
  history 里没有声称写了图的记录），而不只是「抛了异常」。④ 同长度的文本替换在纯几何校验下是**不可见**的——
  盒子宽度逐位相同；要分清哪个闸管哪一段，别声称「都覆盖了」。
- 适用场景：任何「把 A 确定性地转成 B」的编译/落库步骤——先验签名里有没有能让调用方绕过确定性的参数，再问失败是在**提交前**还是**提交后**才被发现。

## 2026-09-23 · 一道叫「语义保持」的闸门会把刚分开的三层身份又合回去（P055-PID-Agent）

- 场景：M7-2 Step 5 我用一道 `semantic_digest_before_layout` 同时覆盖工程事实、renderer 绑定（symbol_key）
  与 layout intent。上一阶段刚刚签署的身份边界是三层：工程语义 / adapter-layout 输入 / canonical layout。
- 结论做法：拆成两道**各自命名**的证明——证明 A 工程语义（tag/class/type/measurement/连接端点·端口·medium/
  方向/system·loop，显式列出 NOT COVER symbol_key 与 layout_intent），证明 B layout input（symbol binding /
  rendering kind / intent / adapter topology identity，用与「引擎收到的 topology」逐项结构相等实现，**不新增身份轴**）。
  两两双向盲区必须有用例：改 intent 或 symbol → A 绿 B 红；改 tag → A 红 B 绿。
- 踩坑点：① 「语义」是个会吸东西的词；一道以它命名的闸门会把不同层的事实重新聚成一个 identity，
  从而撤销前面所有的分层工作。闸门的名字要精确到**是哪一层**的事实。② 顺带的措辞坑：写「布局 digest 只标识位置」
  对几何投影成立、对整个 digest 过强——信封绑定语义输入身份，所以 tag 改、坐标不变时 projection 相同而 digest 必须不同；
  把 projection 的范围当成 digest 的范围，会让一个真实变化被声明成「不可见」。
- 适用场景：任何「保持 X 不变」的证明——先问 X 的边界是否与已分层的身份一一对应，名字错了就会把边界磨掉。

# REUSE_AND_PITFALL_LOG — P055-PID-Agent

## 2026-09-23 · 「两侧独立重建」才叫闸门：同一个函数调两遍只证明了它自己是确定性的（P055-PID-Agent）

- 场景：M7-2 Step 5 要证明「布局没改工程语义」：`semantic_digest_before_layout == semantic_digest_reconstructed_after_layout`。
  最省事的写法是同一个 `plan_semantic_digest(plan)` 在 Step 1 与 Step 5 各调一次。
- 结论做法：两侧**两份输入、两条代码路径、共用一种行形状**——topology 侧从 `SemanticTopology` 读，
  plan 侧从 `plan` 的 live 字段读，只在 `engineering_digest_rows()` 里共用拼装与排序。为此 plan 在 Step 1
  就把收到的工程事实**原样记下来**（systems 含 name/order、entities 含 tag/name/class/type/measurement、
  connection 自带 tag），Step 5 再从这些字段重建。测试先钉住前提："Step 1 做完时两侧行对行相等"。
- 踩坑点：① 如果用同一个函数在同一份数据上调两次，红只会出现在"数据被改"这一种情况，而"数据**少了**一行"
  永远不会红——digest 结构上看不见缺失；所以同一道闸门必须有**纯几何覆盖**的另一半（每个设备恰好放置一次、
  每条连接恰好路由一次、不许凭空多一个 id）。② 覆盖体量的代价要付：把 tag 记进 plan 才看得见 tag，
  把标签文本排除在 canonical 投影之外才不违背"布局身份标识位置、不标识文案"的旧裁定。
- 适用场景：任何"处理前后不许改变 X"的断言——先问"前后两侧是不是同一份输入的两次读取"，是就不是证据。

## 2026-09-23 · 「现在恰好相同」不是合并两个概念的理由：renderer binding 塞进 engineering class（P055-PID-Agent）

- 场景：M7-2 Step 3 要给每个节点一个 catalogue symbol key。当时 `equipment_class` / `instrument_type` 与
  catalogue key **碰巧逐个相同**，直接复用是零成本方案。
- 结论做法：新增显式 `symbol_key`，并把身份拆成两根轴——工程语义 digest（不受 renderer 变化影响）与
  layout/topology digest（必须含 `symbol_key`）。级联升版 `m7-diagram-spec/1→/2`、`adapter-topology-digest/1→/2`、
  `semantic-layout-plan-digest/3→/5`（字段集变了就升版，**未发布的中间版本也不豁免**）。旧 fixture 改成真实
  catalogue 绑定（`pressure_transmitter`→`pressure_indicator`），而不是放宽 production 规则。
- 踩坑点：一个工程类别将来可能有多种合法图形变体，renderer 目录也可能整体重构而不动工程语义；
  一旦复用，"换个等价符号"会同时沉默地改掉两个应该分别回答的问题。
- 适用场景：任何"当前实现里两个概念取值相同"的合并机会——先问"它们会不会各自变化"，会就分开。

## 2026-09-23 · 同一个词「间距」在两个时刻是两条规则：拿已签的约定去重新校验，每张图都会被判坏（P055-PID-Agent）

- 场景：Step 3 要用真实符号尺寸校验放置。第一版直接用"清晰间距"校验**继承自 Step 2 的坐标**，结果
  fixture A 也判不合格：Step 2 的约定是 **origin 间距 = density policy**（`rank_gap(compact)=150`），
  节点宽 120 时清晰间距只有 30 → "清晰间距 ≥ 150"永远不成立。
- 结论做法：把两个时刻分开记名——继承坐标只要求**不重叠**（`MATERIALIZED_PLACEMENT_MUST_NOT_OVERLAP`、
  `STEP_2_ORIGIN_SPACING_IS_INHERITED_NOT_RECHECKED`），重排行才按**清晰间距**校验
  （`REFLOWED_PLACEMENT_MUST_SATISFY_DECLARED_CLEAR_SEPARATION`）。并实测两条路径都可达（A 不重排、
  重排 payload 触发性重排 + 两次运行逐字相同），否则就是一个死分支。
- 踩坑点："校验失败"的第一反应不该是改数据或改算法——先问**这条校验针对的是哪个时刻的产物**。
- 适用场景：任何"上游按约定 A 生成、下游按更严的 B 复检"的流水线。

## 2026-09-23 · 变异第一版全绿，先怀疑 fixture 有没有走到那条分支（P055-PID-Agent）

- 场景：Step 3 标注避让守卫，变异"忽略标签间 clearance"后 **0 条测试变红**。测试在、断言对，
  问题是 fixture 里两个长标签从来不靠近，守卫结构上从未咬合。
- 结论做法：补一条"两个长标签相邻必碰撞"的用例 → 重跑变异 **1 条红**。并区分全绿的两种原因：
  "测试没抓住"（补 fixture）还是"路径不可达"（改断言构造，不补运行时用例）。
- 适用场景：contract + mutation 当防线的项目；"变异不红"至少三种原因——变异是假的 / fixture 没覆盖 /
  路径不可达——必须分别定案后才能宣称守卫已就位。

## 2026-09-23 · 「确定」有两个成本：算法确定 ≠ 排版保真（P055-PID-Agent）

- 场景：Step 3 要判定标注文字尺寸。浏览器 `measureText` 会随 OS 字体与回退而变，因此复用
  `annotation_layout.text_bounds`（纯码点：`max(font_size, len(text)*font_size*0.6)`）。
- 结论做法：记名为 `ANNOTATION_TEXT_METRICS_POLICY = "deterministic_codepoint_extent_v1"` + 改动需升
  `layout_rules_version`；**不**因此新增第三根 digest 输入轴（身份轴保持"算法→rules version、
  renderer 几何→symbol_geometry_catalog_digest"）。黄金用例是固定文本/字号/期望宽度（含一个"字号下限起作用"
  的短标签），**不** hash 源码或字节码。
- 踩坑点：中文与复杂字符宽度迟早要质量升级，但那是 annotation quality 的问题，不该在布局阶段顺手重写
  （会同时移动身份轴与视觉验收基线）。
- 适用场景：把"确定性"写成验收项时，分开记名"输出可复现"与"视觉质量合格"。

## 2026-09-23 · 变异测试本身有四类假货：「只加注释」「变异没接上」「守卫写错方向」「坏声明无报告」（P055-PID-Agent）

- 场景：M7-2 Phase-2B Step 1/2（引擎语义入口 + 确定性放置）要证明「模型已失去几何权威」的守卫是真的。
- **坑 1 · 变异只改了注释**：第一版"引擎假装给节点塞 (0,0)"的变异只加了一句注释，`placement` 仍为空 →
  测试不红。**变异必须真的改变行为**：改成真的写入 `(0,0)` 行之后 5 条测试变红。看到"不红"先怀疑变异，不要先怀疑测试。
- **坑 2 · 变异没接上调用点**：把「放置写回节点坐标」做成变异后 27 条测试全绿——因为写回循环依赖一个
  默认参数，而调用点没传。**全绿也是一条信息**：它说明这条路径在当前数据流下**结构上不可达**。
  于是正确的做法不是补一条运行时测试，而是**把守卫改成断言构造**：`TopologyNode`/`SemanticLayoutPlan`
  是 frozen dataclass、放置函数签名只接受 plan（无 topology 引用）。再验证"去掉 frozen=True → 红"。
- **坑 3 · 断言写错方向**：写出 `assert "preserve_positions" in signature(preview_document).parameters`，
  实际它是 `AutoLayoutRequest` 的**字段**而不是函数参数 → 断言永远为假却"看着像守卫"。
  写边界断言前先 `print` 一次真实形状，别凭印象。
- **坑 4 · 校验器自己不报就崩**：`CANVAS_DERIVATION_CHAIN.index("content_bounds")` 在名字缺失时抛
  `ValueError`，结果是"声明被改坏 → 校验器崩掉"而不是"报一条违规"。**校验器必须先判在不在、再比位置**，
  并且要为这种"缺件"补一条变异测试（我补了 `chain=("semantic_topology","routing")`）。
- 适用场景：任何用 contract + mutation + Gate 当防线的项目：发布"守卫已就位"之前，逐个变异确认**真的变红**，
  并区分"测试没抓住"与"路径不可达"这两种全绿。

## 2026-09-23 · 声明在一边、事实在另一边：新加的边界规则会误杀合法响应（P055-PID-Agent）

- 场景：M7 收尾要把「服务端没评估」（`not_evaluated`，合法业务结果）与「响应压根没带契约」（协议违规）分成两条路。
  前端规则写成「`not_evaluated` 必须带 `global_failure_reason`」，看起来很合理。
- 结果：**写边界测试时才发现**——那个原因字段**只写在证据行里**，`/agent/plan-v2` 的 assessment 上**没有**。
  真实的 revision conflict 响应会因此被判成契约违规，即：**新加的边界规则会把合法回答误杀**。
- 同一模式在前两轮也出现过：v8 表结构（计数必填）与 "未评估" 这个事实冲突；契约说 `m6patch_<digest[:16]>`
  而代码是全长。共同点都是：**规则在一处、事实在另一处**，只有把两端对拍才看得见。
- 可复用做法：给任何新边界规则同时写一条**正向**测试（合法输入必须通过）与一条**负向**测试（违规必须拒绝）。
  只写负向的那一条，会得到一个"什么都能拒绝"的守卫，而它误杀的正是合法流量。

## 2026-09-23 · 没 push 但真实库已经执行过的迁移＝已发布：只能加新版本，不能原地改（P055-PID-Agent）

- 场景：M7 的 schema v9 是本分支上**尚未 push** 的版本，按"未发布即可原地改"的直觉，本可以改 `_migration_9`。
- 反例事实：`data/pid-agent.db`（真实的 109MB 库）`PRAGMA user_version` **已经是 9**。原地改 v9 会让
  **测试新建的库**看到新结构、而**用户手上的真实库**停在旧结构——分歧只会在用户数据上显形。
- 做法：v9 的定义一字不改，新增 `_migration_10` 只做一件事（补唯一索引），幂等且对两种库都成立。测试两头都钉：
  ① fresh 库直接有唯一索引；② 把库改回"v9 且无索引"再跑迁移，索引必须回来、且库仍可写入。
- 规则（可复用）：迁移的"已发布"判据不是 **git 是否 push**，而是 **是否已被任何真实数据库执行**。
  判断方法一行：`PRAGMA user_version`。

## 2026-09-23 · 违规 token 撞上既有功能名：`auto_layout` 把 legacy 报成了新违规（P055-PID-Agent）

- 场景：给 M7-2 Phase-1 写"本阶段不得新增排版表层"的测试，token 里放了 `auto_layout`。
- 结果：测试报出 `preview_auto_layout` / `apply_auto_layout` 两个**早已存在**的 MCP 工具——它们是确定性制图
  那条线的**人工排版路径**。于是"违规列表"里全是假阳，真违规会被埋掉。
- 做法：契约新增 `PRE_EXISTING_LAYOUT_SURFACES` 声明既有表层，并加一条规则：**违规 token 不得是任何既有表层名的子串**
  （不是相等——`auto_layout` 是 `preview_auto_layout` 的子串，相等判断发不现）。能断言的变成"这个集合没有变大"。
- 附带：这和 `preserve_positions` 的分流是同一件事在表层的投影——既有工具就是 legacy 路径，不许改、也不许添第三个。

## 2026-09-23 · 单字段排序键证明不了全序；裸字符串排序键会静默变成字符列表（P055-PID-Agent）

- 场景：canonical projection 声明 `CANONICAL_PROJECTION_SORT_KEY = engineering_id` 同时又声明
  `IS_TOTAL_ORDERED = True`——后者只是**愿望**：不同类型完全可能共享同一个 engineering id，
  那样"canonical 顺序"取决于插入顺序。
- 修法：改成复合键 `(placement_kind, engineering_id)`，并让"唯一性"成为硬失败（重复排序键 = hard fail）、
  未来多呈现行时再用 `presentation_role` 补齐——**全序由数据结构证明，不由布尔常量宣称**。
- 第二个坑（变异测试时发现）：第一版守卫写 `len(sort_key) < 2`，而变异的"单字段"是**裸字符串**
  `"engineering_id"`，长度 13 —— 守卫假绿。字符串可迭代，还会在后续 `for field in sort_key` 里变成一串字符。
  守卫必须显式挡 `isinstance(..., str)`。**再次印证：新守卫要用"应该变红"的变异验收，且变异形状要照着真实误用写。**

## 2026-09-22 · 全绿的 gate 不等于成立的证据：`gate_failures: []` 与 `evidence_verified: false` 可以同时为真（P055-PID-Agent）

- 场景：M6 Phase-2A 收尾跑 acceptance 72 条，命令里忘了 `--candidate-sha`。结果：**八项 gate 全部 `true`、
  `gate_failures: []`、S@1/S@5 与六 family 全部与对照一致**——但 `evidence_verified: false`，
  72 条成功用例**毎条**都带 `success_evidence_incomplete: … missing ['candidate_sha']`。
- 原因：`candidate_sha` 是 CLI 参数（默认空串），acceptance 的种子由它派生。少了它，用例仍能全过
  （种子退化成 `spec::family:index`），于是**跑的是一批“描述不了任何候选”的用例**。
- 更值得记的是**两条断言轴的分离**：`gate_failures` 检查阈值是否达到，`evidence_verified` 检查
  “这条成功声称是否有证据”。**前者不覆盖后者**，所以“gate 绿”不能用来推“证据成立”。
  正确地跑法是 `--candidate-sha $(git rev-parse HEAD)`（CI 里用的正是 `github.sha`）。
- 教训：看到 `gate_failures: []` 就停下，等于只看了一半；同时看 `evidence_verified` 与
  `verification_findings` 的**条数**。两条轴都要绿，而且最好在报告里分两行写。
- 相关但独立：M5 release criteria 要不要把 `evidence_verified` 并进 gate，是一个单独的决策，
  不应在别的里程碑里顺手改（本轮按远端裁定只记 pitfall、不改 gate）。

## 2026-09-22 · 一个“永远会通过”的守卫比没有守卫更危险：引号吃掉了一次真的变异检查（P055-PID-Agent）

- 场景：把五类持久身份的格式从散文提升为契约数据（`PERSISTENT_IDENTITIES`），并加一条守卫：
  “核心源码里不得出现任何身份前缀字面量”。
- 第一版断言写的是**带引号的**形式：`f'"{prefix}"' not in core_source`。而真实字面量是
  `patch_id=f"m6patch_{digest}"`——前缀后面跟的是 `{`，**不是引号**，于是断言永远成立。
- 发现方式只有一种：**把真缺陷改回去看它会不会红**。第一次变异检查输出 `1 passed`，
  我差点把它当成“守卫有效”。改成裸子串断言后同一变异 `1 failed`。
- 教训：**任何新守卫都必须用一次“应该变红”的变异来验收**，而且变异要照着真实代码形状来写
  （这里的真实形状是 f-string，不是普通字符串）。断言一个近似的形状，等于没断言。
- 附带收获：同一轮里“文档说 16 hex、代码写 64 hex”这种**同文自相矛盾**，之所以能活到
  Gate 肉眼核对，正是因为格式写在两处而没有对拍。把格式**降为数据**后，文档对代码的对拍才可写。

## 2026-09-22 · 拷过来的 `ON DELETE CASCADE` 会静默删掉证据；同样的形状在仓库里已有正反两个例子（P055-PID-Agent）

- 场景：给 M6 的 review 聚合建表，最自然的写法是把已有表结构拷过来——而 `document_history` / `agent_sessions`
  写的是 `FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE`。照抄就等于：**删掉一张图纸，
  就把“某人曾确认过这条事实”的历史一起删了**，而那正是审阅需要的记录。
- 仓库里其实同时存在正反两个例子：`document_history` 级联删；而 `audit_records` **故意不指向 `documents`**，
  注释写着 "the evidence for a deletion must outlive the deleted document"。
- 反过来也不能用普通外键（`NO ACTION`/`RESTRICT`）：那会让“存在一条审阅记录”变成图纸删不掉的固因。
  所以正确形式是：**不建这个外键**，只保留一个带索引的 `source_document_id` 列。
- 教训：**“聚合从哪里来”不能靠拷表结构决定**。写外键前先问一句“这条记录的存在依赖于被引用对象存在吗？”——
  图纸与它的历史：是；图纸与关于它的审阅/审计证据：不是。后者必须能活得更久，并且这个选择应该有测试钉住
  （断言 `PRAGMA foreign_key_list` 为空，而不是靠注释）。

## 2026-09-22 · 同一个错犯了两次：把运行时的 volatile metadata 混进身份／契约（P055-PID-Agent）

- 场景（第一次）：A5 的语料身份一开始用 `inspect.getclosurevars` 判断「哪些名字是全局」，那是**运行时的编译
  器**说了算——3.12 内联推导式后同一个函数里的 `__name__` 不再算已解析全局，于是**两边 AST 完全相同、身份
  却随解释器漂**。修法：名字从源码（AST）取，分类从 `co_freevars` 取。
- 场景（第二次，M6）：replay 契约初稿写成「已应用事务的操作**逐字节**比较」
  （`REPLAY_MUST_BE_BIT_IDENTICAL = True`）。它把 `transaction_id` / timestamp / 审计时间这类**合理的
  volatile provenance** 变成了失败原因——正确的重放会因为记账字段不同而报错。
- **同一个错**：把「系统为了记录而生成的运行期字段」当成「被验证对象的一部分」。于是验证要么变成假阴性
  （字节不等 → 明明对的重放被判失败），要么被绕过（“那我们就排除掉……越来越多”），而真正的语义差异反而
  被埋在噪声里。
- 教训（可复用）：任何 digest / replay / 身份契约，**先把字段分成「语义性的」与「记账性的」两列再写代码**，
  然后对**规范化后的语义投影**做哈希（M6 改为 canonical compiled patch digest + resulting semantic
  state digest 两层，并显式声明排除的 volatile 字段）。
- 附带教训：「更严格」不等于「更安全」。逐字节契约看起来更严，实际上它验证的东西不是我们关心的东西；
  严苛用错了对象，会把测试变成噪声源。

## 2026-09-22 · 「谁有写权限」写进数据后，自检第一次运行就抓出了我自己的人工错误（P055-PID-Agent）

- 场景：M6 的治理契约把「哪一层可以写工程模型」当成需要断言的不变式，而不是散文里的一句承诺。写八层链时，
  我给 `apply_v2_transaction`（执行写入的事务）和 `committed_revision`（事务产出的那次 revision）都标了工程写权限。
- **自检当场报错**：`exactly one layer may write the engineering model, found ['apply_v2_transaction',
  'committed_revision']`。
- **真实错误**：`committed_revision` **记录的是结果**，权限属于执行写入的事务，**不属于被它创建的 revision**。
  把「产出物」和「写入者」混为一谈，会让「只有一个写入口」这条不变式永远成立不了（总能找到第二个"写过的层"）。
- 教训一：**「唯一写入者」类不变式要断言集合相等，而不是断言集合里包含谁**——`found == [X]` 能报错，
  `X in found` 不会。
- 教训二：**散文规范里这类错误是不会被发现的**。八层链写成一张表时，两种标法读起来都通顺；只有把权限
  变成可被反驳的数据，它才变成一个当场会红的错误。所以「设计文档 + 契约数据 + 文档↔数据一致性测试」
  这三件套不是重复劳动：文档负责说明为什么，数据负责能被反驳，测试负责两者不脉开。

## 2026-09-22 · 一个标签名字可以把另一个字段的选择器变成“两个元素”（P055-PID-Agent）

- 场景：在 Agent 面板里新增 TypeSafe 区块，字段命名为「TypeSafe Base URL」「TypeSafe API Key」。本地单测全绿，
  但**第一次进 CI 时** Browser acceptance 直接红：`strict mode violation: getByRole('textbox', { name: 'Base URL' })
  resolved to 2 elements`。
- **原因**：Playwright 的 `getByRole(role, { name })` **默认是子串匹配**（case-insensitive，`exact: true` 才是全等），
  所以「TypeSafe Base URL」同时是「Base URL」的匹配项——旧字段（服务预设那块）的选择器从此不再唯一。
  同样的坑对 `Model name` / `API Key` 也成立。
- 教训：新增带标签的字段时，**不要把一个已有字段的 accessible name 包进去**；要么取独特名字
  （「TypeSafe 判读服务地址」），要么用 `exact: true` 的选择器。这段约束以注释形式写在 `App.tsx` 的区块上方。
- 次要教训：这类冲突在本地的**单元测试无法发现**（node 测试不渲染 React），只有浏览器层能抦住；
  所以新增面板字段后应当跑一次 e2e，而不是等 CI。现在有 `frontend/e2e/typesafe-panel.spec.ts` 专门钉字段命名。

## 2026-09-22 · `npx playwright test` **不会重建 dist**：用旧包做的“变异测试”会假装道过（P055-PID-Agent）

- 场景：为证明新写的 e2e 真的能捕捉上面那个标签冲突，我把标签改回旧名字跑测试——结果 **4 条全绿**，看上去“测试没用”。
- **原因**：`npx playwright test` 只启动 `npm run preview`，serve 的是磁盘上**上一次构建的 `dist`**；
  而 `npm run test:e2e` 里才有 `build:e2e`。所以那次变异跑的是**修复后的旧包**，源文件改了但页面没变。
- 正确做法：变异检查必须 `npm run build:e2e`（或 `npm run test:e2e`）后再跑；重做后测试如期变红。
- 推广：任何“改了源码但页面/服务端仍是旧产物”的验证都是假证据。跑浏览器层断言前，先确认产物是本次构建的。

## 2026-09-22 · “环境变量里明明有”与“非交互式 shell 看不到”是两件事（P055-PID-Agent）

- 场景：用户说本机环境变量里有 `TYPESAFE_API_KEY`，而我上一轮报的是“本机没有 key、所以没做真调用”。
- **原因**：它 `export` 在 `~/.zshrc`，而 agent 的终端工具跑的是**非交互式 bash**——两者都不会 source 用户 rc 文件，
  所以 `env | grep TYPESAFE` 在这个 shell 里真的是空的。报“不存在”和“在但继承不到”是两回事，写报告时必须区分。
- 顺带暴露的产品缺陷（已修）：key 只存在于服务端环境时，浏览器无从得知，面板只能显示空输入框，
  用户看到的就是“我配了 key 但界面说没有”。现在面板会问 `/provider/typesafe/status` 并把 `api_key_source` 写出来。
- 取用方式（不打印 key）：`set -a; eval "$(grep -E 'TYPESAFE_[A-Z_]+=' ~/.zshrc | sed -E 's/^\s*export\s+//' | tr -d '\"')"; set +a`。
- 同类坑：GUI/IDE/launcher/`nohup` 启动的服务同样不继承 rc 里的变量；本地真的“拉了服务”却连不上上游时，先查这里。

## 2026-09-22 · 「跨解释器恒定的代码身份」有三个连环坑：`ast.dump` 不是稳定格式 / 空字段 / 匿名定义（P055-PID-Agent）

- 场景：让冻结语料的身份能覆盖“哪个 producer 以什么声明配置画出哪条 case”，而 producer 的行为有一部分
  只存在于函数体里。选“规范化 AST digest”而不是字节码（后者正是 `generator_fingerprint`，换解释器就变）。
- **坑 1 · `ast.dump` 不是跨版本稳定的格式**：同一份源码里的同一个 `_patch`，`ast.dump(node,
  include_attributes=False)` 在 CPython 3.11.15 给出 `930398d67e9220f4`、3.12.12 给出 `a182c517faab2e95`
  ——3.12 给 `FunctionDef` 加了 `type_params` 字段，dump 里就多一段 `type_params=[]`。
  用它当“跨解释器身份”，会造出一个**看起来**干净、实际仍绑解释器的指纹（而且只在 CI 的 3.11 上才暴露）。
  正确做法：自己投一份规范形式（`agentcad/source_identity.py`）——按 `ast.iter_fields` 取字段，**丢掉 `None` 与空列表**，
  再对 sorted-key JSON 取 SHA-256。新版本新增的字段只要还是空的，就不参与身份。
- **坑 2 · “两个解释器都说一样”必须真的跑两个解释器**：验证脚本如果 import 项目代码，就没法在缺依赖的
  第二个解释器上跑。把规范化形式放成**零依赖模块**，验证脚本只 import 它（外加标准库），才能在
  `python3.11` / `python3.12` 上各自复算——不然“跨解释器恒定”只是一句断言。
- **坑 3 · 匿名定义在身份里只会变成 `<lambda>`**：有个 operator 用 inline lambda 重建底座，于是身份表里出现
  一个叫 `<lambda>` 的条目（两个不同 lambda 还会互相覆盖），而“从源码文件复算”的一方无法按名字找到它。
  它同时是一处真重复（同文件早就有那个命名函数，注释还写着“命名是免得两个 operator 漂成两张不同的图”）。
  教训：**要进身份的东西必须能被名字寻址**；顺手把 inline lambda 换回命名函数，行为不变、身份可查。
- **坑 4 · 源码读不到时不要退化成哨兵值**：`inspect.getsource` 失败如果返回 `"unavailable"`，两台机器会
  “同意”一个什么都不标识的数。直接抛错更诚实（`BenchmarkSetupError`），而归档语料那种**真读不到**的情况，
  就明确写成 `ARCHIVED_DEFINITION_IDENTITY = "unavailable: archived declarative definition only"`——
  声明能力边界，而不是伪造历史实现。
- **坑 5 · 身份字段本身也会过期**：第一版 projection 换实现后 digest 从 `471968de…` 变成 `115fe509…`，
  而我把旧值写进了测试常量（跑测试才发现）。凡是“先算、再手抄进测试”的常量，都要**当场从产物里读回**，
  不要凭记忆或从早先的输出里复制。
- 适用场景：任何要发布“跨环境可复算的身份/指纹”的地方（语料、模型、构建产物、schema），
  以及任何想用“打印出来的树”当哈希输入的地方。

## 2026-09-22 · “名字从哪来”也能把身份绑回解释器：`inspect.getclosurevars` 在 3.11/3.12 给出不同的全局集（P055-PID-Agent）

- 场景：A5 的语料身份要包含 producer/case 的代码定义身份。已经在做 AST 规范化（避开 `ast.dump` 的格式漂移），
  自认为跨解释器已稳；本机 3.12 全绿，还加了第二个解释器复算脚本。
- **真正的坑**：身份还折入了“这个定义用到的模块级常量/帮助函数”。我用 `inspect.getclosurevars(fn).globals`
  决定哪些名字属于这一类——而这个集合是**运行中的编译器**说了算：CPython 3.12 内联了推导式（PEP 709），
  同一个函数里一个普通的 `__name__` 在 3.11 被报成“已解析的全局”、在 3.12 就不在集合里。
  于是 digest 依旧随解释器变，而且这个差异 **AST 层看不到**（两边 AST 完全相同）。
- **怎么发现的**：把钉住 golden 的那套测试放到 uv 建的 3.11 环境里跑，5 条挂；再在两个解释器上把
  `core_corpus_projection()` dump 成 JSON 直接 diff，2243 行里只有 1 行不同，定位到一个 safety builder。
  教训：**“我用了 AST 所以跨版本稳”是不够的——只要有一环的信息来自运行时反射，那一环就必须单独证明**。
- **修法**：名字从**源码**取（`ast.walk` 里所有 `Name`/`Load`，包含嵌套作用域），分类用 `co_freevars` + `__closure__`，
  值才去模块命名空间里查。名字集在两台机器上一致，身份才可能一致。
- **顺带的一个产品事实**：安全用例表实际有 13 条，而 spec 里写的 declared count 是 12（`d8b` 是后加的）。
  不要“顺手改成一致”——那个数在冻结 spec body 里，改了会动 `spec_fingerprint`；正确做法是把两个数并列
  展示（投影报 13 条 case + 记录 spec declared 12），把差异交给审阅者。
- 适用场景：任何用反射（`getclosurevars`/`co_names`/`__globals__`/`vars()`）去构造“稳定标识”的代码，
  以及任何只在单一版本上验证过的“跨版本”声明。

## 2026-09-22 · Blender 自动化五个坑：本地化节点名 / save_as 改路径 / 近垂直 TRACK_TO / 影子像变形（P055-PID-Agent）

- 场景：把 P&ID 复现结果推进成 Blender 3D 模型（116 台设备，14 种参数化形体）。通过 blender-mcp 插件的
  `127.0.0.1:9876` socket 驱动（裸 TCP + JSON，`{"type":"execute_code","params":{"code":...}}`）。
- **坑 1 · 界面本地化会毁掉按名字取节点**：中文界面下 Principled BSDF 节点名是 `原理化 BSDF`，
  `nodes.get("Principled BSDF")` 返回 `None`，于是 `if bsdf:` 整段静默跳过 —— 材质全灰但不报错。
  正确做法：**按 type 取**，`next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")`。
  教训：**任何按显示名索引对象的地方，都要警惕界面语言。**
- **坑 2 · `save_as_mainfile` 会改掉会话的文件路径**：直接保存后用户当前打开的文件路径被改指向新文件，
  他下次 Cmd+S 就存错地方。正确做法：加 `copy=True`，只写副本、不动 `bpy.data.filepath`。
- **坑 3 · `TRACK_TO` 约束在近垂直视角退化**：相机放正上方时约束的 up 轴不稳定，俯视图视角异常。
  正确做法：近垂直视角直接设 `rotation_euler=(0,0,0)` 并临时 `constraint.mute = True`。
- **坑 4 · 俯视图里的"几何变形"其实是影子**：低角度太阳（仰角 52°）把 3 m 高的圆柱投影成 2.3 m 长的暗带，
  看起来像圆柱被压扁成胶囊，白查一轮。正确做法：俯视图关 `sun.data.use_shadow`，或把太阳调陡。
- **坑 5 · 渲染引擎名**：Blender 5.2 里没有 `BLENDER_EEVEE_NEXT`，用 `BLENDER_EEVEE`
  （报错信息会列出合法枚举，比猜快）。
- 附带：**在新场景里建、绝不碰用户已有场景** —— `bpy.data.scenes.new()` + 新建 collection + 手动
  `coll.objects.link(obj)`，全程不用 `bpy.ops`（避免它把对象塞进活动场景）。收尾把
  `bpy.context.window.scene` 切回用户原场景。本次用户场景 1019 个对象全程零改动。
- 适用场景：任何通过脚本/MCP 驱动 Blender、DCC 软件或 GUI 应用的自动化任务。

## 2026-09-22 · 用 TypeSafe 把「几何复现」推进到「语义可读」，且不越过 P0（P055-PID-Agent）

- 场景：CAD 导入按 Charter P0 刻意不推断工程语义，所以 9757 图元导入后是 **0 symbol / 0 connector**，
  符号语义映射表仍缺。但图纸自带词表：12 个图层名 + 355 条文字（「燃料盐泵A」「氚分离塔T101」「FCV」「DN50」）。
- 结论做法：用 TypeSafe System One（`jev-latest`）对**每个唯一标注**问两个彼此独立的 Choice 问题——
  `role`（是什么：equipment/valve/instrument/line_spec/system_area/note/symbol_marker/other）
  与 `subsystem`（属于哪部分：salt/gas_supply/cover_gas/offgas/tritium/space_purge/generic）。
  112 标注 + 12 图层，批大小 28、4 个请求并发，**3.11 s / 95.9K in + 18.0K out tokens**。
  state 用结构化对象（`label` / `layer` / `neighbours`），`instructions` 用对象引用 `labels[i]`，
  把「附近标注」作为上下文喂进去——近邻信息显著帮助了 `P`/`F` 这类单字母符号的判断（conf 0.99）。
- 关键经验：**置信度是真信号，不是装饰。** 41/112 两条都 ≥0.90 可直接采用；
  `尾气处理系统？` 因原文带问号掉到 0.31，`干净`、`预留接口` 掉到 0.29/0.32——模型在真不知道的地方说了不知道，
  这批低分行就是人工复核队列。反过来，`DN50` 的 role=1.000 但 subsystem=0.510，
  说明**两个维度要分别看**，不能用一个总分糊过去。
- 边界：产物 `semantics_candidates.json` 只作为**旁挂候选**，不写进 P&ID 文档。
  文档里仍然只有几何/图层/文字/块来源——人工正式审批的边界没有被绕过。

## 2026-09-22 · `import-cad --frame` 的 y 轴是翻转的，按直觉填会溢出画布（P055-PID-Agent）

- 场景：把 `气路系统总图.dwg`（939,381 B / AC1032 / 9757 图元）裁到总图本体，得到可直接编辑的画布。
  按「frame = x0,y0,x1,y1，y0 是上边界」的直觉填 `23780,8160,29480,11670`，画布高度报 3510，
  但元素实际落到 canvas y **1510→4857**——超出画布，图面下半截被切掉、上半截大片空白。
- 根因：`cad_import.py:565` 取 `origin_x, origin_y = frame[0], frame[3]`，即原点是 **(x0, y1)**，
  画布坐标是 `canvas_y = y1 − source_y`。所以 **`y0` 是下边界、`y1` 是上边界**（CAD 的 y 向上）。
  x 轴没有这个问题，所以「只裁 x 不裁 y」的 frame 看起来完全正常，很容易把问题误判成解码器。
- 结论做法：正确写法是 `23780,6760,29480,10210` → 9494 元素、画布 5700×3450、图面填满。
  诊断捷径：先用**只读**的 `POST /imports/cad/plan` 试 frame（毫秒级、不写库），确认 `canvas` 与 `counts.elements`
  再落库；不要直接正式导入试错。另外 frame 只裁画布、**不裁几何**：框外图元被如实计入
  `CAD_PRIMITIVES_NOT_IMPORTED`（本次 263 个），不会静默消失。
- 关键经验：**给「窗口/范围」这类参数，先确认原点和 y 方向，再谈数值。** 只在一个轴上出错时，
  另一个轴的正确会掩盖问题，让人误以为是渲染或解码的锅。

## 2026-09-22 · 图纸的 extents 不等于图纸的内容，游离图元会把画布撑大 10 倍（P055-PID-Agent）

- 场景：`气路系统总图.dwg` 全幅导入后画布是 **58273.8 × 4772.2**，但总图本体只占最右侧
  **5437 × 3347**（x 52837→58274）——导入没有错，是图纸自身的 extents 被撑开了。
- 实测三簇：总图本体 9494 元素；左侧 4347×2705 一片 256 条散碎多段线（全在 `0` 图层）；
  中部 7 条设计问题清单文字（「待解决问题和工作：」等）。后两者合计占画布宽度 90% 却无内容。
- 结论做法：全幅版和「图面区域」版**都要留**——全幅是忠实复现（验收用），裁切版是可用画布（编辑用）。
  判断内容真实范围时别用元素包围盒，先做 **x 方向分桶直方图**：本次 24 桶里有 19 桶为空，
  主簇一眼可见。另外统计点集时必须同时取 `position`、`start`/`end`、`center`、`points`/`vertices`，
  漏掉 `start`/`end` 会把所有线段算成坐标 0，得出「所有线段都在左侧」的假结论（本次踩过）。
- 关键经验：**「图纸范围」是元数据，「图纸内容」是数据。** 二者相等只是巧合，不是保证。

## 2026-09-21 · 只记 fingerprint 不记 body，等于把旧身份变成只能“信”的数字（P055-PID-Agent）

- 场景：M5 coverage-promotion 要把 `BENCHMARK_SPEC_VERSION` 从 2 切到 3（4 个 reachable code 晋升为正式 case）。
  一算就发现：切完之后，所有已发布证据里写的 `spec_fingerprint = c8520c5e…` 在仓库里**再也算不出来**了
  —— v2 的 spec body 只存在于“当时的代码”里，而代码已经变了。远端反复要求的是“发布的东西必须可复算”，
  所以这不是一个可以接受的副作用。
- 结论做法：把旧 spec body **整段冻结**在代码里（`_SPEC_V2_OPERATORS` + `_archived_spec_payload()`），于是
  `spec_fingerprint("2")` 仍返回发布值，并加一条断言把它钅在发布常量上；未知版本**抛错**，不回落到工作 spec。
  归档表故意**不推导**（不用“今天目录减去新 operator”）：那样旧身份会随无关元数据改动而静默漂移。
- 关键经验：**“冻结”的对象是 body，不是数字。** 换成文档也一样：只写“当时是 c8520c5e…”等于把身份变成了信任。

## 2026-09-21 · “旧指纹 + 今天的目录”推出来的旧用例集是假的（P055-PID-Agent）

- 场景：写晋升前后的 case 投影时，先用 `generate_cases(..., spec=spec_fingerprint("2"))` 造“v2 的 72 条”。
  结果“pre”一列里就出现了刚晋升的 `f1_duplicate_label` ——**因为 `spec` 只控制 seed，operator 却是从今天的
  family 轮转里选的**。换言之：那次投影会向审阅者展示一套从未跑过的旧用例集。
- 结论做法：把“旧目录”也变成可读的（`archived_operator_catalogue(version)`），并让
  `generate_cases(..., catalogue=...)` / `operators_for_family(..., catalogue=...)` 接受它；
  `generate_cases(spec=<旧指纹>, catalogue=<旧目录>)` 就能重放旧 case 集。配守测试：
  `test_the_published_v2_case_set_is_still_derivable`（断言旧集里没有任何新 operator）。
- 关键经验：**“用旧参数重跑”必须把决定结果的所有输入都换掉**，只换其中一个（这里是 seed 的那个 spec）
  会得到一套看起来很对、实际不存在的历史。
- 附带教训：靠推导得出的目录字段会在第二个特例出现时变成谎言。`base_variant` 原为
  “有 document_builder 就是 three_valves”，第二个底座（three_valves + 支管）一出现就不对了；现在它是
  `MutationOperator` 上的显式字段，并且**发布在 spec 里**（审阅者读的就是它）。

## 2026-09-21 · 语料指纹一旦含解释器派生的值，“冻结”就变成“在这台机器上冻结”（P055-PID-Agent）

- 场景：coverage-extension 提交 push 之后，CI 的 Backend job 红了，而本地 `pytest` 811 passed。唯一失败是
  `test_extension_corpus_is_frozen`：CI（CPython 3.11.16）算出 `073252f38b4a…`，本地（CPython 3.12）算出
  `c198eb77c731…`。根因不在测试：`coverage_manifest()` 把 `generator_fingerprint` 一起放进被哈希的 payload，
  而 `generator_fingerprint()` 取的是 **CPython 字节码**（`build_base_drawing` 与每个 operator 的 `co_code`）
  ——同一份语料、同一个 SHA，换个解释器就是另一个指纹。`core_corpus_fingerprint()` 同理
  （它也只被断言“不等于 coverage 指纹”，所以没报红）。
- 结论做法：把“语料身份”和“环境指纹”分开。`coverage_corpus_digest()` = 去掉 `ENVIRONMENT_DERIVED_KEYS`
  （`spec_fingerprint` / `generator_fingerprint`）后的 manifest 哈希，它是**跨解释器恒定**的，被测试钉死；
  `coverage_fingerprint()` 原样发布（远端用于核对的 `c198eb77…` 没有移动），但按解释器记进
  `PUBLISHED_FINGERPRINTS_BY_INTERPRETER`（3.11 → CI 实测值、3.12 → 本机实测值），未记录的解释器会
  **明确报错**并提示“先确认 corpus digest 未变，再记录新值”。
- 关键经验：**凡是被当成“冻结”的哈希，输入里不能有任何一个“跑它的机器”字段**。字节码、编译器版本、
  路径、时区都属此列；正确做法是它们继续**发布**（可审计），但不进身份的哈希。另一个教训：`pytest` 全绿
  不等于推送安全——本地 3.12、CI 3.11，只有一条断言会在 CI 上死，而那条断言恰好就是“冻结”的守卫，
  说明守卫本身有价值，只是守卫错了对象。

## 2026-09-21 · 结果哈希的 `exclude` 只作用于顶层，于是它绑定了计时字段（P055-PID-Agent）

- 场景：同一个 candidate SHA 连跑两次 M5 acceptance，`counts` / `sats` / `gates` / 每条 case 记录（去掉 volatile 字段后）
  **逐字段相同**，但发布的 `benchmark_result_hash` 两次不同（`2d827c1e…` vs `e98863ef…`）。原因：
  `repair_digest` → `canonical_digest` → `canonical_payload(model, exclude=...)` 把 `exclude` 直接交给 pydantic 的
  `model_dump(exclude=...)`，而 pydantic 的 `exclude` **只删顶层键**；`REPAIR_VOLATILE_FIELDS` 里的
  `latency_ms` / `timings_ms` / `*_validation_hash` / `document_id` 嵌在 `cases[i]` 里，于是全部进了哈希（实测
  两次运行有 724 处嵌套 volatile 叶子不同）。本地独立验证脚本：`.freebuff/diag_result_hash_determinism.py`。
- 结论做法：暂不动哈希函数（它同时被 M4 的 `release_validator` / `validation_engine` 用来做 provenance，改口径
  等于改 M4 的语义，需远端裁决）。改用**跨运行可比的表示**：把所有 volatile 字段在各层剥掉后求 canonical 哈希，
  两次运行得到同一个值 `a208bdb0…`；coverage-extension track 的 `payload_hash` 本来就是这种递归剥法，所以
  `report_hash` 两次一致（`f56c2c50…`）—— 同一个仓库里已经存在正确做法，冻结语料那条只是没走它。
- 关键经验：**发布一个哈希时，必须能回答“它绑定了什么”**。凡是“exclude / ignore / normalize”这类逃逸口，
  都要用测试固定它在嵌套结构里的行为；否则哈希会在 README 里当“结果指纹”用，而它实际记录的是那天机器的快慢。
- 后续（远端 2026-09-21 批准 a′ 方案）：不改 M4 的 `canonical_digest`，而在 repair 层新增
  `canonical_repair_result_payload()` / `repair_semantic_digest()` 与 `benchmark_semantic_hash`
  （契约版本 `REPAIR_SEMANTIC_HASH_VERSION = "1"`），legacy `benchmark_result_hash` 保留且规则冻结
  （已发布的三份 acceptance payload 仍复算成它们当初发布的值）。实现过程中又多出两条口径：
  (a) **`generator_fingerprint` 也不进语义哈希**——它摘要 CPython 字节码，同一结果在 3.11/3.12 会被算成
  两个结果；(b) **契约版本字段本身不进哈希**，否则“加字段前发布的 payload”与“加字段后发布的 payload”
  永远无法比较，而那正是审阅者最想做的比较（改为在验证器里单独检查版本）。同时候选 30a4339 的三次
  运行（本地 3.12 两次 + CI 3.11 一次）剥 volatile 后得到同一个语义哈希 `2c8000e7…`。

## 2026-09-21 · “写请求没报错”不等于“缺陷真的存在”（P055-PID-Agent）

- 场景：要给 `CONNECTOR_ENDPOINT_POINT_MISMATCH` / `SYMBOL_DEFINITION_MISSING` 这类“有修复策略但 benchmark 造不出”的 code 补 producer。第一版 producer 的做法是“改字段、看有没有抛异常”：`_patch(p1, {"target": {..., "point": 偏移}})` —— `apply_transaction` **返回成功**，revision +1，history 里有一条真实事务，但读回图纸时缺陷**不在**：`_normalize_endpoint` 在 `_prepare_element` 阶段把绑定点从 port 重新导出，未知 symbol_key / 未知 port_id 则直接 `InvalidOperationError`。写面与导入面（`import_document_payload` → `_validate_import_document`）的拒绝方式还不一样：前者会“接受并重算”，后者硬拒。
- 结论做法：把 “representability” 当成一条要机器验证的断言：`probe_representability()` 对每个 code 在两个入口各试一次，分别记录 `accepted`（有没有抛异常）与 `defect_present`（**用 canonical validator 读回图纸、searched by exact code+validator**）两个字段；只有 `defect_present` 才决定“可达”。测试因此断言：写面 `accepted=True` 但 `defect_present=False`（point mismatch）、导入面 `accepted=False` 且 refusal 里带 `stale`，其余两个 code 两面 hard-refuse。
- 关键经验：**布景代码的正确性不能由写调用的返回值决定**：只要系统在写入/导入时会规范化、重算或补齐字段，就存在“写成功、缺陷不存在”的静默分支；只断言“没抛异常”的守卫会把它当成覆盖，最终在覆盖率上多出一个不存在的数。另一个相关坑：同一个模块里 `from x import _symbol` 会被本模块自己定义的同名 `_symbol` 静默覆盖（报错形式是 `TypeError: _symbol() got an unexpected keyword argument`），导入时显式起别名。

## 2026-09-21 · 浏览器套件的“端口覆盖”只做了一半，于是别人的 checkout 变成了被测系统的缺陷（P055-PID-Agent）

- 场景：本机 8000 端口被另一个 checkout 的后端占着（`GET /health` 返回 `P&ID-Agent 2.1.0-alpha.1`）。local-mode 的 `playwright.config.ts` 早就支持 `PID_AGENT_E2E_API_PORT` / `PID_AGENT_E2E_PREVIEW_PORT`，但 `playwright.shared.config.ts` 硬编码 8000/4173（于是 shared 套件直接拒绝启动），而 `e2e/security.shared.spec.ts` 又把 API root 写成常量 `http://127.0.0.1:8000/api/v2`——就算端口改成功，直连 API 的断言也会打到**另一个 checkout 的后端**上。
- 结论做法：把两个 config 都接到同一组端口变量，并让 preview 的 proxy 指向本次 run 真正启动的端口（`PID_AGENT_API_TARGET`）。`security.shared.spec.ts` 也读同一个变量，否则“浏览器走的服务”和“断言走的服务”会是两台。默认值不变，CI 行为不变。
- 关键经验：**一个“可配置端口”只要有一处漏读，整套安全测试的结论就无效**——而且失效方向最糟：拿到的是环境错误，报出来的是“shared 部署有安全漏洞”。凡是端口/地址类配置，要在同一个 PR 里检查 config、spec、proxy、mock 四处是否都走同一个源。

## 2026-09-21 · 一个 TransactionRequest 只能带 1000 个 operation，而 annotation polish 会给每个带 label 的 symbol 再补 2 个（P055-PID-Agent）

- 场景：M5 scale track 要一张几百元素的合成大图，于是把整张图当作**一个**治理事务提交（“一次逻辑变更”，与 CAD 导入的口径一致）。结果：`annotation polish failed for document doc_…: List should have at most 1000 items after validation, not 1023`。那条日志只是 warning，于是真正浮上来的错误是后面的 `assessment invalid`——看起来像校验器拒绝了我的图纸，实际是布景阶段就崩了。
- 结论做法：按 train（一条 8 阀门链）分块提交，每块一个受治理事务。polish 只对**当前还有 label 的** symbol 补 operation，而上一块已经把它们清空，所以每块的 polish 开销有界；总图规模可以任意大。判据不是“元素总数”，而是“单次事务里 operation 数 × 约 2.6”。
- 关键经验：**benchmark/scale 的布景失败会被下游当成被测系统的失败**。看到“assessment invalid”这类结论先回放布景阶段，尤其当有一行 `… failed: …` 的 warning 出现在它前面时——warning 不等于无害。

## 2026-09-21 · `@dataclass` 挂在异常类上会让它无法携带 message（P055-PID-Agent）

- 场景：`repair_benchmark.py` 里 `@dataclass class BenchmarkSetupError(RuntimeError)`。因为它没有字段，生成出的 `__init__(self)` 不接受任何参数，所以 `raise BenchmarkSetupError(f"{label} could not be compiled: …")` 抛出的不是那句说明，而是 `TypeError: BenchmarkSetupError.__init__() takes 1 positional argument but 2 were given`。这条错误路径在全部测试里从未被走到，所以一直没暴露；一旦走到，它把真正的信息完全盖掉。
- 结论做法：异常类一律不加 `@dataclass`（除非它真的带字段），并在文档字符串里写清它不这么做的原因。同类模块（`repair_scale.py` 的 `ScaleSetupError`、`repair_orchestrator` 的 setup error）保持无装饰器。
- 关键经验：**装饰器的默认行为会改变继承来的语义**。给异常、协议类、Protocol 实现加 `@dataclass` 前，先问一句“它还能接住调用者传的那个参数吗？”

## 2026-09-21 · 真实 CAD 文件是几何，不是 P&ID：scale track 必须把这件事说出来（P055-PID-Agent）

- 场景：§G 要求在大图上重跑修复路径，指定文件导入后有 9757 个元素。但一查类型分布：7167 line + 1830 polyline + 398 circle + 362 text，**0 个 symbol、0 个 connector**。修复 operator 需要一个带 port 的元素才能注入 defect——`role_map_for` 直接抛 `ScaleSetupError`。
- 结论做法：不假装某条 line 是 connector。working copy = 真实导入图 + 一条通过 semantic compiler 与受治理事务追加的阀组 train，并在报告里用 `semantic_seed` 字段写明是否使用、原因、加了多少元素；同时提供 `--no-semantic-seed`，拒绝这么做时以退出码 3（setup 失败）而不是“修复失败”结束。另一个方向的证据（纯导入 metrics：SHA-256 / converter+version / import time / element count / base validation time）与原图一起保留。
- 关键经验：**“大图上的成功率”这句话里，“大图”是什么必须先回答**。当源文件只有几何时，把“为可修结构补一层”写进证据，比挑一张恰好有语义的图更诚实。

## 2026-09-21 · `hash()` 不能进 benchmark seed（P055-PID-Agent）

- 场景：scale track 的 case 集是固定列表，所以最初用 `abs(hash(case_id))` 当 seed——“列表固定了，seed 自然固定”。但 `hash()` 按进程加盐，同一台机器两次运行的两个数字不同，evidence 就跑不出一致结果。
- 结论做法：仍然走 `derive_seed(spec, candidate_sha, family, index)`，index 取 case 在冻结列表中的位置。这样“换列表顺序”成为对证据可见的变更，而不是一次静默重掷。
- 关键经验：**任何进 evidence 的随机量都必须是从已发布的输入派生出来的**，即使当前看起来“固定”。

## 2026-09-19 · “分批写入”不是“一次用户动作”：原子性与 undo 必须定义在用户级操作边界上（P055-PID-Agent）

- 场景：CAD 导入最初写成“先 `create_document()`，再按 1000 操作/批 `apply_transaction()`”。每一批都走受治理通道、都有 revision/审计/undo，看起来比“一次性写完”更“规范”。但它有两个真实缺陷：① 第 N 批失败会留下一份**看起来正常**的半张图（revision 正常、列表里有、能打开）；② `undo()` 一次只弹一层栈，所以 9242 图元的大图一次 undo 只能撤最后一批。测试当时全绿，因为测的是“有事务”和“能撤销”。
- 结论做法：把导入做成**一次性逻辑受治理变更**——新增 `DocumentService.create_document_with_operations()`：在内存里把操作序列逐个应用、校验整份文档、然后**只落盘一次**（一个 revision、一条历史、一条审计、一次 undo 快照）。它**复用 `apply_transaction` 真正共享的那部分**：`_stage_mutation()`（操作应用、编辑器分组规范化、revision 约定、结果文档校验）与同一套 provenance/audit 构造器；**持久化是唯一刻意的差异**，因为新建文档没有可冲突的既有 revision、也没有既有 undo 栈，所以这里写一次而不是走事务提交。它没有分块写路径——操作在内存里顺序应用，完成且通过校验后才整体持久化，“分块”这个说法已从代码与文档里删除。补三类负向测试：注入“第 12 个操作失败”→ `list_documents() == []`；>1000 操作 → 一次 undo 清空、一次 redo 全量恢复；历史只有一条且 `operation_count` 等于操作数，另加一条测试断言这条路径与 `apply_transaction` 共用同一个 mutation kernel。
- 关键经验：**物理事务边界 ≠ 用户动作边界**。当实现里出现“for batch in batches: commit(batch)”时，必须回头问一句：用户眼里这是几件事？失败了会看到什么？按一次 Ctrl+Z 会撤掉多少？这三个问题都答不上来，就还没有定义原子性。

## 2026-09-19 · 用户文件名绝不能进命令脚本：staging 用固定内部名（P055-PID-Agent）

- 场景：DWG 解码要把上传写进临时目录中转。原实现用 `Path(filename).name` 派生暂存文件名、又用 `path.stem` 派生 DXF 输出名，而 AutoCAD 路线是**脚本驱动**的（`/s script.scr` 里写 `DXFOUT <输出路径>`）。一个含换行/引号/控制字符的文件名，就能把额外命令插进 AutoCAD 脚本；同一类问题也存在于“把用户路径拼进命令行”。
- 结论做法：暂存、输出、脚本三类路径全部改成固定内部名（`source.dwg` / `output.dxf` / `converter.scr`），转换器永远只被交给 `<workdir>/input/source.dwg`，原始文件名只保留为展示/provenance 字段。回归测试用一个带换行与引号的恶意文件名，断言它**不出现在** argv 与脚本内容里。
- 关键经验：**“这条路径来自用户输入”与“这条路径会进入脚本/命令行”相遇时，必须做名字替换而不是转义**。同时对“公开报告暴露本机路径”做决定：能力清单与导入报告只给基名与 `<workdir>` 标记。

## 2026-09-19 · 生产构建会静默换掉 shared-mode 测试所需的那份 dist（P055-PID-Agent）

- 场景：先跑 `npm run build`（生产模式）再跑 `npm run test:e2e:shared`，本来 2 passed 的共享部署用例开始 45 s 超时——界面一直等不到 `window.__PID_AGENT_E2E__`。看起来像功能回归，实际是 shared 配置**不自己构建**，它复用目录里已有的 dist，而生产构建不含 e2e 钩子。
- 结论做法：跑 shared-mode 之前先 `npm run build:e2e`（或把构建写进该 npm script）。判据：e2e 钩子是否存在，而不是页面“看起来能不能打开”。
- 关键经验：**同一份产物目录被多种模式共用时，构建顺序就是测试的隐式输入**。看到与改动无关的上游失败，先确认产物是哪种模式产出的，再怀疑自己的代码——否则会去“修”一个根本不存在的问题。

## 2026-09-19 · 会生成基线的 workflow 本身不能吞掉失败（P055-PID-Agent）

- 场景：为了在 CI 渲染器里重生成截图基线，新 workflow 里写了 `npx playwright test … --update-snapshots || true`。理由听起来合理：“断言失败是预期的，因为我们在重写期望值”。但 `|| true` 吞掉的不只是像素断言，还有浏览器启动失败、web server 起不来、测试代码抛异常——于是这个 workflow 能产出一份**绿色的、内容为空的“坏基线”**，而 artifact 看起来一切正常。
- 结论做法：去掉无条件 `|| true`，让更新步骤本身成为真门禁；把渲染器从 `ubuntu-latest` 钉到 `ubuntu-24.04`（基线只对产出它的渲染器有意义）；用**与生成步骤绑定的 sentinel** 证明更新真的跑过——更新前**删除**一张**本渲染器**已提交的基线（另存备份），更新后要求它**被重新生成**且比 marker 新；最后再以 **assert 模式**跑一遍，让留在工作树里的基线必须能自证。
- 两个细节是实测踩出来的：① 必须选**本平台**的文件（`*-linux.png`），否则在 Linux 上删掉 darwin 基线等于什么都没删，证明自然不成立；② **不要用逐字节相等**当判据——漂移由 Playwright 的像素容差定义，带时序元素的画面可以在“断言通过”的情况下重新编码出差几个字节的 PNG，逐字节比较只会制造噪声。
- 两次踩坑值得记住：(a) 墙钟窗口（`-newermt '-30 minutes'`）在 fresh checkout 上假通过（git 刚写过所有 PNG 的 mtime）；(b) 只有 marker、不制造变化，则在**正确的无漂移运行**上假失败（Playwright 只重写真正不同的快照）。正确修法是“让任务自己制造它要观察的那个变化”，而不是把门禁放宽回墙钟窗口。
- 关键经验：**“预期失败”只适用于你明确知道会在哪一层失败的部分**。给整条命令加吞错，等于把“我知道这里会红”扩写成“这里红不红我都不看”。凡是生成 artifacts 的流水线，都要单独验证“失败时它确实会红”。

## 2026-09-19 · provenance 声明必须由“审计记录里的精确值”测试证明（P055-PID-Agent）

- 场景：文档里写着“导入记录源 SHA-256”，代码也确实把 SHA 写进了 `document.metadata.cad_import`。但 `create_document()` 的审计 evidence 只写 name/canvas/history_source，所以“SHA 同时进入审计”这个承诺并不成立——而 metadata 是可以被后续修改的，审计链才是契约凭据。
- 结论做法：给审计证据加上服务端**从实际解码字节算出**的 `cad_import` 绑定块（sha256 / format / converter / converter_version / operation_count），并写精确值断言：`record.evidence["cad_import"]["source_sha256"] == hashlib.sha256(data).hexdigest()`，同时断言 `record.document_id` 与 `record.result_revision`。
- 关键经验：**“记录在某个地方”不等于“记录在承诺的地方”**。凡文档里出现“同时进入 X”这类句子，测试就要精确地在 X 里找那个值；只断言“存在一个 sha 字段”的测试不能防住这类偏差。

## 2026-09-19 · 非均匀仿射变换会改变图元类型（圆→椭圆）（P055-PID-Agent）

- 场景：块的 INSERT 带 `scale=(3, 1)` 时，块里的圆在图纸上其实是椭圆，但导入把它当圆重建，只用了“行列式缩放”得到一个标量半径——图元类型错了，而且看不出来（圆还是圆）。
- 结论做法：重建“原生形状”之前先判断变换类别（`matrix_is_uniform`）；非均匀时改为对**完全变换后**的曲线采样成闭合折线，并报 `CAD_CIRCLE_APPROXIMATED`。配一个非均匀缩放测试与一个均匀缩放的对照测试。
- 关键经验：**几何保真优先于图元类型保真**。任何“把采样曲线还原成原生图元”的优化，都要先确认变换是相似变换；否则宁可用折线。

## 2026-09-19 · 想接“官方 MCP”之前先看本机装了什么：AutoCAD 自带的无界面引擎比 MCP 更有用（P055-PID-Agent）

- 场景：用户装了正式版 AutoCAD，要求“找个官方 MCP 接上”。调查结论：Autodesk 官方公开的 MCP server 是 **Revit / Model Data Explorer / Fusion Data / Product Help** 以及 ACC/Forma 平台侧服务，**没有 AutoCAD 桌面版官方 MCP**；社区那些 AutoCAD MCP 全是第三方，且依赖 Windows 的 COM/.NET/文件 IPC，在 macOS 上根本无法驱动 AutoCAD。即使可用也不该接：一个直接改 DWG 的外部服务会绕开本项目“唯一受治理写通道”的模型（Charter §7 / P0-2），等于开第二条无审计写路径。
- 结论做法：不接写手，接**解码器**。实测发现 **AutoCAD for Mac 2027 自带 `AcCoreConsole`**（`AutoCAD <年份>.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole`）——官方无界面核心引擎，支持 `/i 输入 /s 脚本`。用它 `DXFOUT` 出 DXF，再交给本项目的自研 DXF 读取器：同一张真实图纸得 **9757 个原生图元、0 个块定义缺失、3.7 s**，而 LibreDWG 的 `dwg2dxf` 只有 6523 个图元并丢掉 157 个块定义（正好是图上最显眼的 140 个阀门）。
- 两个集成坑（都已写成测试）：① AutoCAD 命令行里**空行等于重复上一条命令**，脚本里的空行会让 `DXFOUT` 重新进入、把控制台卡在等输入上（进程不退出→超时）；② `accoreconsole` **不支持 `--version`**，探测版本只能读它启动横幅，而且读完必须把它杀掉（它没有脚本时永不退出）。
- 关键经验：**“能不能调起来”跟“调起来有没有用”是两件事**。在接入任何外部 AI 桥（MCP/插件/插件市场工具）之前，先检查本机已安装的官方工具是否本身就提供无界面入口——这次官方引擎的保真度直接超过所有第三方解码路径，且不引入新的依赖与授权问题。

## 2026-09-19 · 装了重型工具的开发机会悄悄改变测试结果（P055-PID-Agent）

- 场景：把 AutoCAD 接为转换器候选后，三个“没有可用解码器时应该报 no_dwg_converter”的测试开始失败——不是因为逻辑错，而是这台机器现在真的装了 AutoCAD，于是“找不到解码器”这个前提不成立了。
- 结论做法：把“声明式搜索位置”做成可按名字整组替换的 `EXECUTABLE_GLOBS`，所有相关 fixture 直接 `monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})`；并额外用“通配符找不到就是找不到”的测试固定住“不会伪造路径”。
- 关键经验：**凡是以“环境里没有 X”为前提的测试，都必须显式关掉 X 的发现路径**，否则它的绿/红取决于开发机上装过什么——CI 与本地会给出不同的结论，而两边都以为自己在测同一件事。

## 2026-09-19 · “单调”必须写清楚跟谁比：参照点写错，一个阶段就能把上一阶段的成果吐回去（P055-PID-Agent）

- 场景：M3 的整理引擎承诺“每个阶段只有不使任何硬性图面指标变差时才被接受”，文档、findings、测试都在说这一句，但代码里 `commit()` 比较的是 **`before`（整轮开始前的输入快照）**。于是“与输入比没变差”被当成了“没有把上一阶段的改善吐回去”：A 把某硬指标 5→1，B 把 1→4，旧实现因 4 ≤ 5 而放行。测试全绿，因为测的都是“最终输出 vs 初始输入”和“一个阶段直接比初始输入更差”。
- 结论做法：参照点改为**最近一次被接受的状态**（`current`，只在阶段被接受时前进，并直接复用该阶段的候选快照，不重算当前图，成本与旧版相同），`DRAFTING_ENGINE_VERSION` 1→2（接受语义变了，摘要必须能区分两种语义）；同时保留“最终 vs 输入”作为第二层保险（`DRAFT_RESULT_REGRESSION`）。回归测试直接抄评审给的计数例（5→1→4 时 B 必须回滚），并逐次断言“比较参照点只能是最近接受态”。
- 关键经验：**描述同一件事的两句话可能指向不同的实现**。“不使任何硬性指标变差”这种主语缺失的句子，必须补上“相对于谁”——否则代码读起来完全自洽。凡是“逐步改进/逐步收敛”的算法，验收时都要问一句：**每一步跟谁比？**
- 真实数据实测（同一活库快照，v1/v2 两个 worktree）：修订后 448 图元图纸提交的操作从 179 降到 81，差额正是旧实现放行过的回退阶段；耗时 45.7 s → 40.4 s。**更严的契约会减少引擎实际动作**，这是预期后果而非退化：硬指标之间不再允许此消彼长，而要让两者兼得应该改成“部分接受”（只落地不冲突的那部分改动），而不是把契约改回宽松。

## 2026-09-18 · 合成的测试 fixtures 找不到真实数据的坑：新功能上线前先跑一遍活库（P055-PID-Agent）

- 场景：M3 的整理引擎有 48 个单测 + 离线 harness 全绿，e2e 也新增了 3 个用例。但把界面指向**真实项目库**后，24 张非空图纸里有 2 张让 `POST /drafting/preview` 直接 500：它们在历史版本/其它 checkout 里引用了当前图例库已不存在的符号（`pressure_transmitter`），而 `DocumentService` 对这类元素会正当地拒绝计算端口坐标（`symbols.get` 的 `KeyError`）。只读路由因此把“数据缺陷”变成“服务错误”。
- 结论做法：把“不可用元素”变成一等概念（`unresolvable_element_ids` = 未知图例的符号 + 绑定到它的管线），从所有 pass 的候选集中排除；报告里给 `DRAFT_SYMBOL_DEFINITION_MISSING` 警告（不阻断门禁），**锁定取证保持为空**（不能让“图例库缺失”冒充“工程师锁定”）；同时保留两道安全网——阶段级 `try/except InvalidOperationError`（在副本上工作、只提交完成的阶段，因此可以直接停在最后一次成功提交）与 `service._symbol_port_point` 的 `KeyError` → `InvalidOperationError` 转换。修复后 24/24 返回 200。
- 关键经验：**fixtures 只证明你想到的形状**。一个只看合成图纸的流水线，会稳定地在你自己的真实数据上第一次运行时报错。新增任何“读整张图”的能力后，第一件事就是拿活库/样本库跑一遍并记录结果（要只读！本项目两个整理路由都是 read 路由，所以这一步是安全的）。

## 2026-09-18 · “不修”也可以是对的，但必须能被机器验证（P055-PID-Agent）

- 场景：真实数据的一轮实测发现，大部分图纸整理后产出很少操作甚至 0 操作（多为“阶段回滚/找不到完整避障路径”）。很容易把报告写成“引擎很保守所以没效果”，或者反过来为了好看而放宽门禁。
- 结论做法：把“不动”也当作一种有据可查的结论——`DRAFT_STAGE_ROLLED_BACK` / `DRAFT_STAGE_UNAVAILABLE` 逐条列出原因，指标块展示 before → after（含不变项），`settled` 明确表示“再跑一次也不会变”。同时把实测数字写进文档（24 张图纸：17 张产出事务、7 张已收敛、11 张分数提升、**0 回归**、中位数 4.2 s、最大 48 s）。
- 关键经验：**“为什么没改”和“改了什么”同样是需要证据的输出**。另外要区分两种“无改善”：① 已经有据可查地达标（好事）；② 引擎能力边界（待办）。把它们写在同一句“无改动”里，等于把待办藏起来。


## 2026-09-18 · 自动“美化图面”的第一性要求是单调：阶段不达标就整段回滚（P055-PID-Agent）

- 场景：M3 把排布、重路由、标签摆放、跨线桥接、碰撞分离串成一条流水线后发现，每一段单独看都在“改好”，合起来却可能把图越弄越糟（挪走一个设备 → 原来最优的管路失效 → 管线穿设备）。
- 结论做法：把“什么算变差”写成一个数据结构（`DRAFTING_HARD_FIELDS`：穿设备 / 节点重叠 / 微小线段 / 斜线 / 越界 / 保留区侵入 / 悬空连接点 / error 数），每个阶段结束时比较 before/after，**只有没有任何硬指标变差才提交**，否则整段回滚并记 `DRAFT_STAGE_ROLLED_BACK`；结果若仍有回归，再补一条 `DRAFT_RESULT_REGRESSION` blocker。
- 关键经验：自动排版工具的“变好看”不能靠自我评价，必须**把判据变成可枚举的数据 + 可回滚的提交边界**。这也让测试可以写成强断言（`hard_regressions(before, after) == []`），而不是“看起来还行”。
- 连锁收益：因为回滚判据存在，各 pass 可以放心地互相干扰——流水线重复到不动点（`pipeline_rounds`），并用 `settled`（再跑一次无事务）作为收敛的可验证定义。

## 2026-09-18 · 锁定必须是“图纸数据”，且必须报告来源（P055-PID-Agent）

- 场景：自动整理最容易造成的事故是“把工程师刚刚人工排好的部分挪了”。把锁定做成请求参数（`locked_element_ids`）最省事，但它随请求结束而消失，下一个人再跑一次整理就把人工成果推平。
- 结论做法：三种来源合成一个冻结集，并逐条回报来源——① 本次请求；② `element.metadata["drafting_lock"]`（编辑器里的“钉住”，跨会话、跨调用方）；③ `metadata.layout_regions` 中 `kind="lock"` 的区域（“这块已确认，绕着它排”）。回报结构 `DraftingLocks` 分字段列出每类来源，另有 `skipped_locked_element_ids` 记录“想动但因锁定而跳过”。
- 关键经验：**“谁冻结了它”是可审计事实，不能只留一个布尔标志**。另外，同一条锁定语义必须接进所有会动几何的引擎（本项目 `auto_layout` 与 `drafting_engine` 共用 `_locked_element_ids`），否则“什么算锁定”会出现两种理解。
- 附带坑：解锁/锁定要写成**普通受治理事务**（改元数据），这样 revision 校验、审计与 undo 全部免费继承；一旦为“锁定”开一个私有写接口，就等于给自动流程开了第二个写入口。

## 2026-09-18 · 几何相交不等于工程相连：跨线只能打一个桥，且只能打一边（P055-PID-Agent）

- 场景：两条管线在图上相交，工程上可能完全无关（跨线），也可能是显式 `junction`（真的分支）。若把“交点”直接当成连接，或把桥随便打在主线上，会把工程语义画错。
- 结论做法：跨线桥**只在次要线**上打（优先级：声明流向 → 折点少 → 更短），每条跨线**恰好一个**桥（双桥会被收敛成单桥），落在 `junction` 上的跨线报 `DRAFT_CROSSING_ON_JUNCTION` 而不是“顺手抹平”；悬空连接点报 `DRAFT_JUNCTION_DANGLING`；**引擎永不新增/删除/重绑拓扑**，整理事务的 patch 一旦出现 `source`/`target` 即报 `DRAFT_TOPOLOGY_CHANGED` blocker。
- 关键经验：Charter P0-4（连接性是语义、不是样式）在实现层的落点就是这条：**“看起来连在一起”和“连在一起”必须走两条不同的代码路径**，并且能被测试分别验证。

## 2026-09-18 · 保留空间要“既绕又改”：只会事后抱怨的检查器，会稳定地产出被抱怨的图（P055-PID-Agent）

- 场景：图例/标题栏/禁布区是声明在 `metadata.layout_regions` 的真实几何。第一版整理只能“穿过去然后报 `DRAFT_RESERVED_REGION_OVERLAP`”，于是每跑一次都稳定地产出一处可修但没修的缺陷。
- 结论做法：保留区同时进入**评分**（走线把它当障碍物）与**修正**（标签优先避开；新增 `reserved_eviction` 阶段把未锁定的设备/连接节点移出保留区）。对自己修不动的那部分（被锁定的侵入者）保留 blocker，而不是把它藏起来。
- 关键经验：如果一个检查器只会在事后报警，那它就是**一份稳定的缺陷生产线**。校验规则的每一项都该问一句：**“引擎能不能在生成阶段就避免它？”** 能避免就必须进评分/修正，不能只进报告。

## 2026-09-18 · 用 git worktree 做 A/B，把“大概率是存量问题”变成证据（P055-PID-Agent）

- 场景：新增功能后本地 Playwright 有 2 个视觉快照失败，M2 时也失败。很容易写“应该是存量问题”，但验收方上一轮已经明确不接受未经证实的“应该是”。
- 结论做法：`git worktree add .freebuff/m2base <accepted-baseline>` 建出上一个已验收 commit 的工作树，把 `frontend/node_modules` 用**软链接**指回复用（免去重新装依赖），在同一台机器同一次运行中跑同一批失败用例，直接比较差异像素数。本轮实测：基线 `22941` / `22574`，本轮 `22868` / `22501`（ratio 都是 0.02）——同一批失败、同一量级。
- 关键经验：**“不是本轮引入”是一句需要测量的话**。worktree + 软链依赖是一个几分钟就能完成的 A/B 手段，比在报告里写“应该是既有问题”便宜得多。用完 `git worktree remove --force` 清理。

## 2026-09-18 · 先 `npm run build` 再直接 `npx playwright test` = 用生产 bundle 跑 e2e（P055-PID-Agent）

- 场景：为验证前端改动，先跑了 `npm run build`，然后直接 `npx playwright test e2e/drafting.spec.ts`，结果 4 个用例失败，其中两个此前是绿的。看着像功能回归。
- 根因：`npm run test:e2e` 会先执行 `build:e2e`（`vite build --mode e2e`），e2e 专用桥（`installE2EBridge` 等）只在该模式下注入；直接用生产 bundle 启动 preview 时页面进不了预期状态，`editor-canvas` 不可见。
- 结论做法：永远用 `npm run test:e2e -- <spec>`（或先手动 `npm run build:e2e`），不要用生产 build 跑 e2e；出现“连画布都看不到”的失败时，**先怀疑构建模式，再怀疑代码**。
- 关键经验：测试脚本里的“前置构建”不是仪式，它定义了被测物。绕过 npm script 直接调底层 CLI，很容易悄悄换掉被测对象。


## 2026-09-18 · “可复现”的摘要必须把算法版本算进去（P055-PID-Agent）

- 场景：M3 要求“同样内容得到同样结果”。如果摘要只哈希输入与输出几何，那么某次改动算法后产出不同但内容哈希一致的中间态时，“同一摘要”很容易被误读成“同一算法”。
- 结论做法：`transaction_digest` 一起摘要 `engine_version + request + input/output content hash + 规范序操作`；规则哈希只对工程内容（剔除 style）计算；各 pass 在 **id 规范序快照**上运行，操作以规范序输出，因此摘要与被发现顺序、存储顺序无关（测试遍历：打乱元素存储顺序仍得到同一摘要）。
- 关键经验：**可复现性是有前提的契约，不是口号**——把前提（引擎版本、输入、算法）写进摘要里，“同摘要 = 同算法同输入同结果”才是真的；改算法时就该提 `ENGINE_VERSION`，否则你提供的是假的不变量。
- 附带收益：把引擎的输出再喂回引擎应当 `settled`（无事务），这条幂等性断言比“再跑一次看着一样”强得多。

## 2026-09-18 · 文档里“唯一实现”的话，要么做成真的，要么删掉（P055-PID-Agent）

- 场景：新模块的 docstring 写着“端口坐标只有一份实现，`DocumentService._symbol_port_point` 已经委托到这里”——但 `service.py` 里其实还有一份**手写副本**（数学一模一样，只是迟早会不同）。这种不一致不会让测试变红，但会让下一个改端口几何的人只改一处。
- 结论做法：把 `DocumentService._symbol_port_point` 真正改为委托 `drafting_geometry.symbol_port_point`（函数内局部 import，避免底层模块长出一条指向上层几何的依赖边），并用一个断言把两份结果钉在一起（测试直接比较 `resolve_ports` 输出与 `service._symbol_port_point`）。
- 关键经验：**“单一实现”是可以被验证的声明**。写 docstring 时如果“委托”二字背后没有 import，那不是设计，是愿望；要么补齐，要么不成此语。

## 2026-09-18 · 以数组引用为依赖的 React effect：一次普通编辑会把用户踢出分析面板（P055-PID-Agent）

- 场景：右侧面板新增「整理」tab 后，用户在面板里点“锁定”（一次普通事务），页面立即跳回「属性」tab：“选中项变了就显示属性”的 effect 被重新触发。
- 根因：effect 依赖 `state.selectedElementIds`（数组引用）。任何事务后 store 都会重新生成选中数组（`filter` 存活 id），于是“同一次选择”变成了“新选择”，effect 把面板强行切走。
- 结论做法：以**选中内容**为 key（`selectedElementIds.join("\u0000")`）而不是数组引用，并用 e2e 守住“锁定后仍停在整理面板”（否则这种回归只能靠人眼发现）。
- 关键经验：React 的依赖数组不能放“会被无害地重建”的引用；“每次编辑都重建”的 state 数组配 `useEffect` 就是一台自动跳页机器。分析型面板（只读、需要停留）与画布选择（主动导航）必须用不同的事件源。

## 2026-09-18 · 派生图层永远只能“可证明地新鲜”（P055-PID-Agent）

- 场景：M2 需要把工程语义图做成可复用的项目级产物（跨图 OPC、项目统计、后续 Agent context），但持久化缓存一旦被当成真值，就会出现“索引说 3 台泵、图纸实际 4 台”的静默错误——这类错误在工程交付里比崩溃更危险。
- 结论做法：缓存行同时保存 `revision + content_hash + builder_version`；新鲜度分两级且**命名诚实**：只有比较 revision 的叫 `fresh`（必要条件），真正重算 content hash 的才叫 `verified_fresh`（承诺）；失效原因逐条列出（`revision_changed` / `content_hash_changed` / `builder_version_changed` / `document_deleted`）；读取入口 `graph()` 先验证再返回，发现过期就重建；项目级读入口把过期/孤儿行变成 `IR_INDEX_STALE` / `IR_INDEX_ORPHAN` finding，而不是把旧数字当新数字返回。
- 关键经验：**派生数据要么可证明新鲜，要么显式承认过期**，没有第三种状态。“缓存命中率”不能凌驾于“工程数字不能错”。同时：缓存不进审计链（可重算的东西不是证据），但**显式重建命令要审计**（“谁在什么时候重算了项目索引”是真实运维事实）。
- 实施细节：`document_content_hash` 只哈希工程内容（元素/图层/系统，剔除 style），因此改颜色不让索引失效、改位号一定失效；`builder_version` 变化令所有行失效，避免“新旧逻辑混合的图”。

## 2026-09-18 · 工程标识应由 tag 决定，重复 tag 必须报错而不是合并（P055-PID-Agent）

> ⚠️ **本条的身份规则已被推翻**（M2 第二次验收，2026-09-18）：tag 是可变工程属性，不能兼任永久主键。现行做法见下方「稳定身份不能由可变 tag 兼任」。本条的“重复 tag 绝不合并、降级要如实标注”仍然有效。

- 场景：工程对象需要跨 revision、跨导入/导出的稳定身份，但 element id 是随机的（导入会重新生成）；而真实图纸又常出现重复位号。
- 结论做法：对象 id 优先由工程位号派生（`valve:hv-101`），无位号时降级为 element id，并用 `identity_scope`（`tag` / `element`）如实标注用的是哪一种；同一文档内重复 tag **绝不合并**，第二个及之后按 element id 排序加确定性 `#2`/`#3`，同时报 `IR_DUPLICATE_IDENTITY`。
- 关键经验：图上两个阀共用位号是**工程缺陷**，不是别名机会；把“看起来一样的对象”合并成一个是把缺陷藏起来。身份降级要写在数据里（`identity_scope`），不要藏在文档里。
- 附带收益：测试可以断言“重导入后对象 id 集合不变”，这正是 IR 能否作为长期工程主线的根本判据。

## 2026-09-18 · 别让结构启发式凭空造出工程语义（P055-PID-Agent）

- 场景：信号线分类很容易写成“一端接仪表就是信号”，但 P&ID 上的**工艺引压/取样点**同样一端接仪表——按该规则会把工艺管线误判成信号。
- 结论做法：信号分类以**显式介质命名**为主（signal/electric/pneumatic/impulse/信号/电/气动…），仅保留一条保守结构规则（两端都是仪表对象）；其余一律留在 process，并把纯结构判断（如“已声明信号却没接仪表”）变成 finding 交给审查。
- 关键经验：启发式可以**发现问题**，不可以**创造语义**；宁少勿多，漏判会被人看到，误判会被 Agent 当真。

## 2026-09-18 · 用“活体枚举 + 声明式契约”证明没有隐藏写路径（P055-PID-Agent）

- 场景：T0.5 后写路径都接上了审计与权限门，但没人能保证以后新加的 endpoint 不会绕过去；靠代码评审回答“还有没有旁路”不可持续。
- 结论做法：新增 `surface_contract.py`（声明式数据，不在导入时授予任何权限）+ 测试从**实时 OpenAPI schema 与实时 MCP 源文件**枚举全部 mutating 路由和 tool 名，双向比对：未声明 → 失败，声明了不存在 → 也失败；“声明为只读”的路由必须附带一个**否定测试**证明它真的不写（例如 `canvas-grid` 不产生 revision）。
- 关键经验：治理机制要能被机器反证。声明式契约 + 活体枚举 + 否定测试，三件套缺一不可；否则契约文件本身会先腐烂。

## 2026-09-18 · 测试里的版本号硬编码会把“旧迁移测试”变成假测试（P055-PID-Agent）

- 场景：v3 迁移测试写的是 `PRAGMA user_version=CURRENT_SCHEMA_VERSION - 1`；schema 从 v4 涨到 v5 后，该测试实际构造的是“谎称 v4 但缺 audit_records”的不可能数据库，迁移直接报缺表——测试失败暴露了它一直没有真正测试 v3→v4。
- 结论做法：迁移测试必须**显式写出目标旧版本号**（`user_version=3` / `user_version=4`）并构造该版本真实具备的表结构；对每个新增 schema 版本补一条“旧版本 → 当前版本”的迁移测试，并断言迁移不会凭空回填历史数据（不伪造证据）。
- 关键经验：测试里凡是“相对当前版本推算出来的历史”，都会随着版本上涨而失真，但未必立刻报错；这类隐式耦合应在 review 时直接改写为显式常量。


## 2026-09-18 · 审批对象要从 JSON Patch 升级为 Semantic Diff（P055-PID-Agent）

- 场景：Approval Gate 已能严格绑定 exact intent，但工程师若只能看到原始 TransactionRequest / JSON patch，仍然很难判断“到底改了什么工程内容”。
- 结论做法：保留 low-level history snapshot 作为 forensic truth，在其上新增 deterministic Semantic Diff 层，把 symbol/connector/junction/text 等结构化变化解释为 valve/equipment/instrument/pipeline 等工程实体变化，并生成 field-level before/after 与可读 summary。
- 关键经验：工程 Agent 的 review object 应是“工程变化”，不是模型 prompt，也不是 raw JSON；低层 diff 与语义 diff 应分层保存，前者保证精确审计，后者服务审批、人机协作和 Agent repair。
- 风险边界：Semantic Diff 的 risk_hint 只能作为 deterministic review hint；安全等级、SIS/PSV/联锁等正式风险判断必须由项目 Rule Engine 与工程标准决定，不能让启发式分类替代工程规则。
- 实施方式：先提供无写入 preview + revision 持久化 + REST/MCP 共用 contract，再在 T0.5 把 diff hash 和 validation evidence 绑定到 approval/provenance。


## 2026-09-18 · Approval 必须绑定 exact intent，不能只批准工具名（P055-PID-Agent）

- 场景：Tool Registry 已能标记 ask/deny，但若 approval 只记录“用户批准 apply_transaction”，同一个批准令牌就可能被换成另一份事务、另一张图或另一个 Agent session 使用。
- 结论做法：Approval 绑定 `session + tool_name + document_id + canonical intent hash`；执行前重新计算 SHA-256 比对；成功执行后状态变为 consumed，禁止重放。Tool Call 另外记录 base/result revision 和 error status。
- 关键经验：工程 Agent 的“人工确认”不能只是前端按钮状态，必须是后端可验证、持久化、与具体工程变更严格绑定的 capability token。
- 旁路治理：不能只保护高层 semantic endpoint；任何模型可访问的低层 MCP mutation API 也必须复用同一 gate，否则 Agent 会自然选择阻力更小的旧工具绕过治理。
- 人机边界：普通人工编辑器事务仍可直接进入 DocumentService；Agent-originated engineering change 才强制走 Harness Approval Gate，避免把治理层错误地变成所有 UI 点击的额外负担。
- 适用场景：CAD/CAE、数据库变更、基础设施、金融操作等需要“模型可自主规划，但关键写入由人明确批准”的 Agent Harness。


## 2026-09-18 · Tool Registry 先做元数据层，不重写事务执行器（P055-PID-Agent）

- 场景：按 PROJECT_CHARTER Priority 0 启动 Agent Harness 改造；仓库已有成熟的 DocumentService、TransactionRequest、SemanticTransactionCompiler、MCP/REST 能力，若直接重写执行路径风险很高。
- 结论做法：先新增 canonical Tool Registry，只统一机器可读的 schema、permission、risk、side-effect、preview、idempotency、audit event 和 surface；REST、MCP、semantic planner 共用同一 catalog，现有执行器和原子事务边界保持不变。
- 关键经验：Harness 抽象应先“包住”稳定工程内核，再逐步把 permission/session/audit 接入执行门；不要为了架构漂亮而先推翻已验证的事务系统。
- 风险控制：只登记已有真实执行路径的工具；Charter 中尚未实现的理想工具不能先写进 registry 冒充能力。工程变更型 semantic apply 先声明 ask，下一阶段再做 enforcement。
- 验收：增加 registry 单测、REST/semantic schema 同源测试、MCP catalog 同源测试，并由 CI 执行 ruff/quality-harness/pytest/frontend/e2e。

## 2026-09-18 · 用 Canonical Charter 约束未来模型重规划（P055-PID-Agent）

- 场景：项目从“AI 可编辑的结构化 P&ID 软件”进一步明确为面向工程交付的 Agent Harness；未来模型能力会持续升级，单纯维护临时 Roadmap 容易发生目标漂移。
- 结论做法：新增根目录 `PROJECT_CHARTER.md`，把最终工程交付目标和 P0 不可变原则与可变技术路线分离。模型可以优化 IR、工具粒度、数据库、UI、Multi-Agent 等实现，但不能自行取消 semantic-first、受控工具修改、deterministic validation、audit/rollback、model-agnostic 和人工正式 release approval。
- 关键经验：长期 AI 项目不能只写“功能路线图”，还需要明确 immutable core、mutable architecture、benchmark、release gate 和 replanning governance；否则模型越强，越可能高效地偏离最初工程目标。
- 实施原则：先在现有 DocumentService + TransactionRequest 上增加 Tool Registry，不做大爆炸重写；优先完成 Session、Permission/Approval、Semantic Diff、Audit，再逐步升级 Engineering IR 和 project graph。
- 适用场景：任何需要多年迭代、会被多代 Agent 接力开发、同时又有强工程质量/合规责任边界的项目。
- 待同步：工作区总库 REUSE_AND_PITFALL_LOG.md 不在当前 GitHub 仓库连接范围内，本轮仅完成项目内日志。

## 2026-08-21 · 左侧侧边栏双容器滚动冲突根除、全域滚轮穿透与导航标签体系（P055-PID-Agent）

**场景**：用户反馈当图纸较多或点开分类文件夹后，其它文件夹与图纸被向下挤出视口且无法用鼠标滚轮向下滑动，且侧边栏未见清晰可用滚动条。

**结论做法**：
1. **根除嵌套滚动陷阱（Scroll Trap）与全域滚轮穿透**：
   - 彻底移除了 `.document-tree-list` 内部的 `max-height` 和 `overflow-y: auto` 嵌套滚动限制，使图纸目录在侧边栏自然铺展；
   - 将外层 `.sidebar.documents-panel` 设为唯一主滚动容器（`overflow-y: auto` + `scrollbar-gutter: stable` + `scrollbar-width: thin`），并在明暗双主题下配置高对比度滑块，使鼠标在侧边栏任意位置滚动均能平滑穿透滚动到底。
2. **左侧面板导航分栏与一键折叠（`App.tsx` / `DocumentTree.tsx`）**：
   - 顶部提供 `[ 全部 ] [ 📁 图纸 (N) ] [ 📐 图例 ]` 3 档视图切换，支持一键切换到专属满屏图纸管理视图；
   - 工具栏新增 `⊞ 展开全部 / ⊟ 折叠全部` 快捷按钮，支持一键收拢所有分类以便迅速定位目标文件夹。
3. **单位图例收敛与基础图元过滤（`SymbolPalette.tsx`）**：
   - 自动过滤 `category === "基础图元"`，左下角纯净化为工程物项，基础图元由顶部工具栏专职负责。

**验证**：前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，后端 `pytest -q` **271/271 全部通过**，E2E 测试 **42/42 全绿**。

**适用场景**：复杂 CAD 侧边栏多树状节点滚动优化、滚动陷阱消解与分栏导航。

（已同步总库 2026-08-21）

## 2026-08-21 · 大模型流式传输阻塞根因排查与 Starlette ASGI 中间件改造（P055-PID-Agent）

**场景**：用户使用 Ollama / MiniMax M3 Cloud / DeepSeek / Kimi 等模型执行生成时，遇到「额度消耗但无反馈、界面长时间转圈、流式规划未返回有效结果」的严重问题。

**结论做法**：
1. **消除同步阻塞请求与重复调用**：
   - 彻底移除了 `semantic_planner.py` 中流式连接前多余的同步探测性 `client.post`，直接使用单次 `client.stream("POST", ...)` 建立连接，避免首字生成前耗尽额度与产生长时间阻塞。
2. **Starlette BaseHTTPMiddleware 流式冲突与 ASGI 中间件重构**：
   - 排查发现 FastAPI 挂载的 `BaseHTTPMiddleware`（如 `@app.middleware("http")`）在 `StreamingResponse` 进行 `listen_for_disconnect` 时，因底层 TaskGroup 协程读写冲突会抛出 `RuntimeError: Unexpected message received: http.request`，直接强行切断 SSE 连接并导致前端 500。
   - 将 `RequestDiagnosticsMiddleware` 与 `RequestBoundary` 全面重构为原生纯 ASGI 中间件（`async def __call__(self, scope, receive, send)`），彻底根除 TaskGroup 冲突与伪 `http.request` 重复投递。
3. **主流大模型思考链多协议统一分流**：
   - 针对 Ollama / MiniMax 的 `"reasoning"`、DeepSeek / GLM 的 `"reasoning_content"`、OpenRouter 的 `"reasoning"` 以及内嵌 `<think>...</think>` / `<thought>...</thought>`，统一由状态机分流至 `thinking` SSE 通道，实现秒级逐字打字涌现。

**验证**：使用本地 Ollama 与 MiniMax M3 Cloud 真实联调端到端 P&ID 出图流式测试，100% 成功生成储罐与离心泵标准对齐管线，质量评分 100.0 分；后端 `pytest -q` 271/271 全部通过，前端 `npm test` 98/98 全部通过。

**适用场景**：FastAPI / Starlette SSE 流式端点与全局安全/诊断中间件集成、主流推理模型思维链统一分流提取。

（已同步总库 2026-08-21）

## 2026-08-21 · 基础图元顶部分类快捷栏（免属性弹窗）与大模型思考/生成双流式传输（P055-PID-Agent）

**场景**：用户提出两项工程交互增强需求：① 基础图元（如云线、六边形、八边形、菱形、圆柱、机柜、气泡、粗箭头等）不同于复杂工业设备/阀门，放置时无需弹出工程属性配置弹窗；且希望将其置于顶部工具栏分类收纳，支持直接拖拽至下方画布；② 在大语言模型执行自然语言规划时，希望能够实时流式看到模型的深度思考过程（Chain-of-Thought / Reasoning）与输出草案内容。

**结论做法**：
1. **基础图元免属性弹窗与顶部菜单栏分类快捷工具栏（`BasicShapesToolbar.tsx` / `engineeringProperties.ts`）**：
   - 在 `engineeringProperties.ts` 中实现 `isBasicShapeSymbol()` 判定；在 `EditorCanvas.tsx` 中当检测到基础图元时跳过 `requestItemProperties`，拖入/点击放置立即直接落图。
   - 顶部工具栏新增 `BasicShapesToolbar` 下拉分组菜单：分为「几何逻辑」、「设备容器」、「标注流向」、「管道附件」4 个子分类；每个图元卡片均设置 `draggable={true}`，支持鼠标直接拖拽放置到下方画布，或点击选中后单击画布落图。
2. **大语言模型思考过程与输出流式传输（`plan-v2-stream` / `AgentStreamingViewer.tsx`）**：
   - 后端新增 `POST /documents/{document_id}/agent/plan-v2-stream` SSE 端点，在 `SemanticAgentPlanner` 中实现 `_stream_model_json` 与 `stream_plan_events`；同时兼容提取 `delta.reasoning_content`（DeepSeek-R1 / Qwen 等）和 `<think>...</think>` 内嵌思考标签。
   - 前端新增 `api.planSemanticAgentStream` 与 `AgentStreamingViewer.tsx` 响应式流式组件，以双折叠卡片（🧠 思考过程 / 📝 生成草案）配合打字光标和呼吸指示灯实时呈现模型思维链。
3. **踩坑点**：
   - SSE 流式端点须加入 `security.py` 的 `_is_agent_planning_path` 白名单，确保鉴权与生命周期管理一致。
   - Playwright 测试拦截规划请求时，路由匹配规则应使用 `**/agent/plan-v2*` 通配符兼容普通请求与流式请求。

**验证**：后端 `pytest -q` **271/271 全部通过**，前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，Playwright E2E 全量测试 `npm run test:e2e` **42/42 100% 全绿**（含新增 `basic-shapes.spec.ts`）。

**适用场景**：CAD/绘图软件顶部快捷图元分类栏、免属性快速落图交互、LLM Agent 推理流与思考过程实时可视化。

（已同步总库 2026-08-21）

## 2026-08-21 · 项目分类文件夹树状图纸归档与通用基础图元库移植（P055-PID-Agent）

**场景**：用户反映散落的图纸过多无法分清归属项目，要求在左侧支持新建项目文件夹（分类）并按装置/工段/系统归档管理图纸；同时从 draw.io 移植通用工业常用基础图元（变更云线、六边形位号框、八边形安全框、菱形判定框、立式圆柱体、立方体机柜撬块、梯形下料槽、平行四边形IO框、引线说明气泡、工艺粗指示箭头、8字盲板、阻火器、管道视镜等）。

**结论做法**：
1. **项目分类文件夹体系与树状图纸管理（`DocumentTree.tsx` / `FolderDialogs.tsx`）**：
   - 文件夹列表维护在 `projectSettings.metadata.folders`（`id`, `name`, `created_at`）中，天然随项目包导入/导出持久化；图纸归属记录于 `Document.metadata.folder_id`。
   - 左侧侧边栏引入 `DocumentTree` 分组手风琴折叠树：支持一键新建分类、重命名分类、删除分类（关联图纸自动转入「未分类」）、分类内图纸数量 Badge、以及下拉框一键跨文件夹快速移动。
   - 新建图纸弹窗（`CreateDocumentDialog.tsx`）支持直接下拉指定所属项目分类。
   - 顶部集成实时双向搜索过滤框（支持模糊搜索图纸名与文件夹名）与一键清空 `✕`。
2. **draw.io 核心基础图元库移植（`standard_symbols.json`）**：
   - 新增 `"基础图元"` 顶层分类，涵盖变更云线 `revision_cloud`、六边形位号框 `hexagon_tag`、八边形安全框 `octagon_box`、菱形判定框 `diamond_decision`、立式圆柱体 `cylinder_vessel`、立体机柜撬块 `cube_cabinet`、梯形沉降槽 `trapezoid_hopper`、平行四边形IO框 `parallelogram_io`、引线标注气泡 `callout_bubble`、工艺粗指示箭头 `block_arrow_right`，以及管道附件 8字盲板通/断、防爆阻火器、管道视镜等。
   - 所有图元严格符合 Quality Harness 的 `_SUPPORTED_SHAPES` 闭合路径规范与端口流向规范。
3. **踩坑点**：
   - `DocumentSummary` 模型需显式返回 `metadata: dict[str, Any]`，以便前端 `/documents` 列表接口单次请求即可直接获得每张图纸的 `folder_id`，无需对每张图纸发起单独的详情查询。
   - 文件夹创建/重命名弹窗严格遵循项目 SOP 规范，禁止使用原生的 `window.prompt()`，全面采用应用内受控对话框（`FolderDialogs.tsx`）。

**验证**：后端 `pytest -q` **271/271 全部通过**，前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，Playwright E2E 全量测试 `npm run test:e2e` **41/41 100% 全绿**（含新增 `project-folders.spec.ts`）。

**适用场景**：多图纸大型工程项目分类归档树、文件管理交互设计、通用工程几何图元库扩充。

（已同步总库 2026-08-21）

## 2026-08-21 · 属性面板三段式架构与图例库悬停放大镜预览（P055-PID-Agent）

**场景**：借鉴 draw.io 优秀的 Format Panel 与图例 Sidebar 交互，完成右侧属性面板三段式分类（`全部` / `⚙️ 工程` / `🎨 样式` / `📐 排列`）重构，并在排列面板中集成快捷对齐、等间距分布、编组与编辑锁；左侧图例库新增悬停 Popover 放大预览、分类徽章、尺寸与端口清单展示，并为图例搜索框添加一键清空按钮。

**结论做法**：
1. **右侧属性面板三段式重构（`PropertyInspector.tsx`）**：
   - 顶部提供 `[ 全部 ] [ ⚙️ 工程 ] [ 🎨 样式 ] [ 📐 排列 ]` 4 档快速视图切换；默认「全部」视图呈现完整参数卡片栈，点击单个分类可聚焦对应工程/样式/几何视图。
   - 排列面板中集成多选快捷对齐（左/中/右/顶/中/底）、等间距均匀分布（水平/垂直）、编组/解组与图层锁定按钮，一键通过原子事务 `update_element` 提交更新。
2. **左侧图例库悬停放大预览与搜索优化（`SymbolPalette.tsx`）**：
   - 鼠标悬停图例卡片时，经过 350ms 防抖自动弹出高清 Popover 预览卡片，包含大号矢量预览、物项类型 Badge、标准尺寸、端口方向与介质清单，移出即销毁。
   - 搜索框内嵌快捷 `✕` 一键清空按钮，提升搜索筛选效率。
3. **踩坑点**：
   - 属性面板内部分类按钮勿使用 `role="tab"`，避免与页面顶层 5 大主面板 Tab（`[role="tab"]` `属性 1` 等）产生 Playwright Strict Mode 定位冲突。
   - 属性面板使用条件分块渲染与全量 `all` 默认态，既保障单选/多选表单数据统一无漏提交，又完全兼容自动化测试对隐藏输入框的访问。

**验证**：前端 `npm test` **98/98 全部通过**，`npm run build:e2e` 0 报错，Playwright `npx playwright test` **40/40 100% 全绿**，后端 `pytest -q` **271/271 全部通过**。

**适用场景**：复杂 CAD 属性面板三段式布局、图元悬停放大与端口提示、表单无缝原子持久化。

（已同步总库 2026-08-21）

## 2026-08-21 · 工业标准图元库扩充与智能磁吸等间距吸附体系（P055-PID-Agent）

**场景**：吸收 draw.io 优秀图例与交互算法体系，扩充化工与流程工业核心标准图元（如角阀、三通调节阀、减压阀、螺旋板换热器、精馏塔、旋风分离器、袋式过滤器、螺杆泵、罗茨风机、文丘里混合器），并增强管线跨线桥控制与画布智能磁吸对齐（Smart Guides / 等间距吸附 / 放置吸附）。

**结论做法**：
1. **标准图元库扩充（`standard_symbols.json`）**：
   - 提取并新增 10 个高频工业图元，严格遵循 `SymbolDefinition` 规范，所有闭合截面使用规范 `path`（`d="M ... L ... Z"`）而非非标准的 `polygon`，确保与 Quality Harness、DXF 分层导出、CairoSVG 渲染 100% 兼容。
   - 所有端口（`ports`）设置严格的方向性（`in` / `out` / `bidirectional`）与介质类型（`process` / `drain` 等）。
2. **智能磁吸与等间距吸附（`editorGeometry.ts`）**：
   - `snapSelectionToGuides` 新增多目标中心间的等间距候选计算（`source: "equidistant"`），当移动元件到两相邻元件的等距位置（`dist(A, B) == dist(B, C)`）时，自动触发磁吸并以翠绿色（`#10b981`）呈现等间距辅助参考线。
   - 新增 `snapPointToGuides(point, targetRects, tolerance)`，在图元拖入画布放置（`onCanvasDrop`）时即时磁吸到已有设备中心或边缘。
3. **踩坑点**：
   - AgentCAD 后端 `quality_harness.py` 对图元形状有严格的 `_SUPPORTED_SHAPES = {"line", "polyline", "rect", "circle", "path", "text"}` 静态白名单检查，图元形状切勿使用 `polygon`，应使用标准 SVG `path`。

**验证**：后端 `pytest -q` **271/271 全部通过**，`ruff check agentcad` 0 报错，前端 `npm test` **98/98 全部通过**，Playwright e2e `npm run test:e2e` **40/40 100% 全绿**。

**适用场景**：P&ID 标准图例扩展、CAD 智能参考线与等间距吸附算法、端到端图形质量门禁。

（已同步总库 2026-08-21）

## 2026-08-21 · 物项专属工程属性弹窗与下拉自填配置体系（P055-PID-Agent）

**场景**：用户拖拽或点击放置不同物项（如阀门、设备、管线、仪表、管件/跨图等）到 P&ID 画布时，需要弹出分类专属的工程属性配置对话框；涵盖出入口管径（DN50/DN100/1/4"等标准）、公称压力、材质、故障安全位置（FC/FO/FL/FI）、常态位置（NO/NC/CSO/CSC）、量程与信号制式、设计参数等；所有字段支持广域预设下拉与自由自填兼备（非必填，可一键跳过），并在右侧属性面板实时同步与修改。

**结论做法**：
1. **分类专属 Schema 与标准预设库**（`engineeringProperties.ts`）：
   - 基于 GB/T、HG/T 20559、ASME B16.5、ISA 5.1 标准，建立 5 大类专属 Schema（`valve` 阀门、`equipment` 过程设备、`connector` 工艺管线、`instrument` 过程仪表、`node_fitting` 管件/跨图）。
   - 预设涵盖公称通径（DN10 ~ DN500，1/8" ~ 20"）、压力等级（PN10 ~ PN100，Class 150 ~ 900）、材质（304/316L/碳钢/钛/哈氏合金/PTFE 等）、驱动与故障位置、信号制式（4-20mA+HART/总线）等。
2. **受控弹窗宿主与 Combo-box 交互**（`ItemPropertyDialogHost.tsx`）：
   - 拖拽与点选符号放置时唤起受控弹窗，展示分类 Badge 徽章、位号输入、专属参数双列网格。
   - 所有字段使用 HTML5 `<input list="...">` + `<datalist>` 兼顾模糊下拉预设与自由输入自填，支持「直接跳过」、「确认并放置」与「取消」。
3. **右侧属性栏双向同步与原子持久化**（`PropertyInspector.tsx`）：
   - 元素选中时根据类别自动渲染专属工程参数组，修改通过 `buildPropertyPatch` 将属性结构化存入 `element.properties` 与通用兼容 `element.metadata`。
4. **踩坑点**：
   - `<input list="...">` 元素在无显式 `htmlFor` 时，Playwright 默认角色为 `combobox` 而非 `textbox`，应使用 `getByLabel` 定位，并通过 `<label htmlFor="...">` 与 `<input id="...">` 建立显式关联以增强可访问性。
   - 弹窗确认按钮与取消按钮的 accessible name 避免包含相同子串（如「取消放置」包含「放置」可能引起 Playwright strict mode 冲突），分别命名为「取消」、「直接跳过」、「确认并放置」。

**验证**：前端单测 `npm test` 96/96 通过，Playwright 端到端测试 `npm run test:e2e` **40/40 100% 全绿**（含新增 `item-properties.spec.ts`），后端 `pytest` 271/271 全部通过，`ruff check agentcad` 0 报错。

**适用场景**：工程 CAD/P&ID 专属物项参数录入、HTML5 原生下拉与自填组合控件、前端受控表单弹窗体系。

（已同步总库 2026-08-21）

## 2026-08-21 · 取消超时硬顶与随时手动叫停机制落地（P055-PID-Agent）

**场景**：本地大模型（如 Ollama、LM Studio 或私有部署量化模型）推理与规划耗时较长，原有的 120s/600s 超时上限会导致本地大模型规划经常被系统截断中断；用户要求取消超时设置，改为默认一直持续，同时新增各操作的即时「停止 / 叫停」按钮供随时人工干预。

**结论做法**：
1. **取消超时硬顶，默认无限持续**：
   - 后端 `Settings` 将 `agent_timeout_seconds` 默认值设为 `None`（取消 `<= 600` 的强制硬编码检查，允许环境变量指定或留空表示无限等待）。
   - `ProviderConfig.timeout_seconds` 设为可选 `float | None`，`httpx.Client(timeout=provider.timeout_seconds)` 当 timeout 为 None 时默认不中断连接，满足本地大模型长生成需求。
   - 前端从 Agent 高级设置中彻底移除数字超时输入框与有效上限显示，不强加硬性截断。
2. **全链路 AbortController 与即时叫停交互**：
   - 前端 `api.ts` 的 `planSemanticAgent`、`replanSemanticAgent`、`testProvider`、`listProviderModels` 统一接收 `signal?: AbortSignal`，在 `authorizedFetch` 中监听并在捕获到 `AbortError` 时平滑转为用户友好的取消响应。
   - 手动模式生成中（`planningAgent`/`repairingAgent`）与自动执行中（`AutomaticAgentRunner`）均提供醒目的「🛑 停止生成」/「🛑 叫停」按钮，点击立即调用 `abortController.abort()` 释放连接，清空加载态并优雅提示「已手动停止生成」。

**验证**：后端 `pytest` 271/271 全部通过，`ruff check backend` 0 报错，前端 `npm test` 92/92 通过，`npm run build` & `build:e2e` 通过，Playwright e2e `npm run test:e2e` **39/39 100% 全绿**。

**适用场景**：本地慢速大模型接入、长时 Agent 规划任务、前端 fetch 实时取消（AbortController）与优雅状态恢复。

（已同步总库 2026-08-21）

## 2026-08-21 · 零个人配置商业化交付与厂商解耦纯净化（P055-PID-Agent）

**场景**：彻底完成商业化纯净化改造（零个人配置交付，任何用户部署/开箱即用，动态获取模型，彻底移除第三方私有域名、具体厂商名如 Kimi/DeepSeek/Groq 等硬编码 Preset 与默认模型名，源码与文档 100% 厂商中立），并修复存量 e2e 测试（effective timeout 180s 校验与 OPC 双击跳转）。

**结论做法**：
1. **零默认模型与 100% 厂商解耦标准**：
   - 彻底废除源码中的任何具体厂商命名（如 Kimi）与预设模型（如 `kimi-for-coding`），`providerPresets.ts` 仅保留标准「OpenAI 兼容端点」、「Ollama 本地服务」、「LM Studio 本地服务」、「自定义端点」，且 `defaultModel` 全部置空/移除。
   - 所有外部 LLM 端点、Token 限制、推理模型判定全部转为标准通用协议与环境变量配置（如 `PID_AGENT_REASONING_MODELS`），前端通过 `/models` 动态发现模型或用户手动输入。
   - 提供标准化 `.env.example`，文档中将专有厂商教程替换为通用的 OpenAI-compatible 规范。
2. **Playwright 双击事件在有副作用的单次 handler 下失效**：双击操作（`dblclick`）会在首次 click 时触发 `onClick`。如果 `onClick` 触发了局部状态改变（如选中元素导致侧边栏/属性栏展开，引起 SVG 画布容器 resize 或微位移），第二次 click 会落在屏幕不同坐标上，浏览器将无法聚合产生 `dblclick` 原生事件。**做法**：对于需要支持双击跳转或编辑的透明命中层（如 OPC jump target），单次点击不要触发改变布局的副作用，让双击与捕获监听器纯净触发；同时 JSX 文本中不要把 `&` 误写为 `&amp;`（React 会渲染字面量 `&amp;` 导致选择器失配）。
3. **Playwright webServer 环境变量注入**：`document-creation.spec.ts` 会校验服务端的有效超时配置，需在 `playwright.config.ts` 的 `webServer.env` 中显式注入 `PID_AGENT_AGENT_TIMEOUT_SECONDS: "180"`。

**验证**：后端 `pytest` 270/270 通过，`ruff check` 0 报错，前端 `npm test` 92/92 通过，`npm run build:e2e` 通过，全量 e2e 测试 `npm run test:e2e` **39/39 首次全绿**（含 10 张视觉快照回归），全局 `workspace-check.sh` 0 error。

**适用场景**：商业化项目交付纯净化、LLM 协议通用化与厂商解耦、Playwright e2e 双击交互排障、React SVG overlay 事件模型设计。

（已同步总库 2026-08-21）

## 2026-08-20 · 青创大赛路演 PPT 生成与"无视觉模型"验证法（P055-PID-Agent）

**场景**：依据项目计划书 docx 用 PptxGenJS 生成 18 页路演 PPT（深色科技风），嵌入氩气 P&ID、国标阀门图例与编辑器 e2e 截图；本会话模型（deepseek-v4-pro）无图像输入，且宿主 describe-image 未配置视觉模型 baseURL，无法直接看图。

**结论做法**：
- PPT 生成链路：pitch/deck.js（pptxgenjs + slides skill 的 helpers：imageSizingContain / warnIfSlideHasOverlaps / warnIfSlideElementsOutOfBounds）→ LibreOffice 渲染逐页 PNG（render_slides.py，注意本机 soffice 不在 PATH，需 PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"）→ slides_test.py 溢出检测 → 用 macOS Vision 框架写 swift OCR 脚本逐页核验文字 → PIL 逐行像素高度验证 chip 单行渲染。
- 中文字号换行估算：CJK=1.0 单位、ASCII=0.62、空格=0.35，宽度=(units*pt/72)；chips 文本需留 0.42in 内边距并避免" / "空格连排，否则窄药丸内折行溢出。
- OCR 验证清单：每页标题/关键数字/里程碑标签（M1-M5）/底部技术栈 chips 全部命中；light 文字(>160 灰)与面板边框(<150 灰)用不同阈值区分行高。

**踩坑点**：
- **write 工具写 JS 时模板字面量会吃掉 \n 转义**：deck.js 内含 '\n' 的字符串被写成真实换行导致 SyntaxError，需事后 edit 修补或写入时用 \\n。
- **imageSizingContain 返回不带 path**，addImage 需自行拼 { path, ...contain }。
- **warnIfSlideHasOverlaps 对"文字压面板/圆点压✓"必报 severe**：属正常卡片设计，在代码处注释"有意设计"即可，真正的文字互压（声明盒相交）才需修。
- **describe_image 在本宿主不可用**（describe-image baseURL 未配置），read_image 因模型非多模态被拒；本机验证方案=OCR+像素统计，能覆盖"字在不在/是否折行/是否越界"，但不能审美判断，最终版式需人工过一遍 montage.png。
- **fonts**：全篇指定 Microsoft YaHei（Windows/WPS 命中）；macOS LibreOffice 渲染自动替代为冬青黑体，全角等宽版式不变。

**适用场景**：无视觉能力的会话里生成并自检 PPT/海报等图文产物；中文 PPT 换行与 chip 宽度估算；slides skill 的渲染/校验脚本在本机的 PATH 与字体坑。

（待同步总库）


# 本项目踩坑记录（REUSE_AND_PITFALL_LOG）

本项目专属的踩坑/可复用经验明细。总项目库 `/Users/joe/ai/reasonix/REUSE_AND_PITFALL_LOG.md` 为全局汇总。

规则：执行任务前先读本文件；任务完成后**双写**——先追加本文件，再同步追加总库（见 AGENTS.md）。

已有记录：

## 2026-08-19 · UI token 化重构与 e2e 环境坑（P055-PID-Agent）

**场景**：分支 `ui-polish-2026-08-19` 对前端做整体视觉重构（美观简洁大方），只改 CSS 不动 JSX/TS，并保持浅/深双主题与 e2e 视觉回归通过。

**结论做法**：
- 重构策略：`styles.css` 整体重写为设计 token 驱动——`:root` 定义浅色 token、`.app-shell[data-theme="dark"]` 覆盖为深色 token；各文件早已引用但从未定义的 `var(--border)/var(--panel-background)/var(--muted-text)` 等变量顺带生效。工程画布配色（#69778a/#9ba8b8 与 SVG 元素色）严格不动。
- 验收：临时 Playwright 截图脚本（浅/深/命令面板/Agent 面板四张）人工比对 → `test:e2e:update` 重生成 10 张快照 → 全量 e2e。

**踩坑点**：
- **Playwright webServer 端口冲突要杀子进程**：`kill` npm/uvicorn 包装进程后，vite preview 与 uvicorn 的子进程仍占着 4173/8000，`reuseExistingServer:false` 直接报错；必须 `lsof -nP -iTCP:8000 -sTCP:LISTEN` 找到真实 PID 再杀。
- **Playwright 浏览器版本严格绑定**：本机缓存有 chromium-1223/1234，但项目 @playwright/test 1.61.1 只认 1228，需 `npx playwright install chromium`；临时截图脚本可用 `executablePath` 指向其他缓存版本应急。
- 临时 node 脚本放 `/tmp` 会 ERR_MODULE_NOT_FOUND（ESM 从脚本所在目录解析依赖），必须放进 frontend/ 目录内。
- e2e 两个存量失败（基线同样失败，与 CSS 无关）：`document-creation`「effective timeout」期望上限 180s，但 playwright.config.ts 的 webServer env 未设 `PID_AGENT_AGENT_TIMEOUT_SECONDS`（后端默认 600）；`flow-runtime`「OPC double click」跳转失败根因未查。UI 改动验收前先跑基线对照，避免把存量失败算到自己头上。

**适用场景**：大型 CSS token 化重构、Playwright webServer/浏览器版本排障、UI 回归的基线对照方法。

（已同步总库 2026-08-19）

## 2026-08-06 · 全量 review + 修复后提交（P055-PID-Agent）

**场景**：接管 main 分支 63 文件未提交修改（+5118/−373，codex_handoff 称"已准备推送"但实际未提交），两轮 review 子代理 + 未跟踪新文件审查 + 人工验证，修复 4 中危 + 7 低危后分组提交。

**结论做法**：
- 中危修复：`diagram_quality.py` ① `_element_rect` 旋转符号用绕中心旋转的精确包围盒（0/180° 快速路径）；② `_segment_intersects_rect` 非正交段用 Liang-Barsky 裁剪（要求 t1-t0>EPSILON，保持"严格穿过"语义）；③ `route_connector_points` 交叉计数用按主轴排序的一维段索引 + 二分定位（预排除共享端点连接），`_port_direction`/`infer_flow_direction` 增加 element_map 参数复用；④ `svg.py` `embedded_off_page_label` 坐标由硬编码 (47,30) 改为按定义尺寸推导。
- 低危：`_expand_compact_cabinet_branches` 画布底部空间不足时整体上移组而非压缩高度（8 分支场景不再重叠溢出）；`_place_compact_nodes` 候选枚举上限=到画布边界最大距离；`_request_model_json` 用显式 `repair` 参数替代 `user_prompt.startswith("Schema repair attempt:")` 前缀判定（基类调用点传 repair=True）；compiler polish 异常 `pass` → `logging.warning`；`deleted_text_ids` 排序；前端 renameCurrentDocument 补 catch（`messageFromError` 从 store.ts 导出）。
- 提交分组：feat:quality+标准阀门图例 / feat:vision+SVG+agent runtime / feat:frontend / chore:规则与脚本。

**踩坑点**：
- **本机没有 vitest**，`npx vitest run` 会临时拉取 vitest 4 且与项目 `node:test` 风格测试不兼容 → 33 文件全报 "No test suite found"。项目前端测试必须用 `npm test`（`node --experimental-strip-types --test tests/*.test.ts`）。
- review 子代理环境无 git diff/pytest 权限，只能精读文件；未跟踪新文件（diagram_quality.py 等）review 工具看不到，需另开子代理审查。
- `svg.py` 元素 label 的旋转抵消 `rotate(-θ, anchor)` 以锚点为旋转中心时，位置跟随符号旋转且文本保持正立——绕自身抵消是正确模式；embedded label 旧硬编码 (47,30) 与定义中心 (50,25) 不一致导致旋转 180° 偏移 (6,−10)。
- 旋转包围盒测试用例几何要先手算验证（首版对角段 (50,50)→(400,400) 实际不穿过矩形 (200,100)-(260,140)，测试失败是用例错误而非实现错误）。
- 显式参数重构会破坏直接调用私有方法的测试（test_k3_schema_repair 用字符串前缀模拟契约），需同步更新测试。
- `multi_edit` 跨文件编辑（semantic_compiler_engine + annotation_layout 混在一起）因目标文件错误整体回滚——**multi_edit 只用于单文件**，跨文件用并行 edit_file。

**适用场景**：大型未提交工作批次接管、绘图质量检查模块几何正确性、前端 node:test 测试环境识别。

（已同步总库 2026-08-06）

## 2026-09-18 · e2e 套件会清库：不能用活的项目库跑验收（P055-PID-Agent）

- 场景：想在本机顺手跑 Playwright 验收新面板，`frontend/e2e/fixtures.ts` 的 `resetDocuments()` 会遍历 `GET /documents` 并逐个 DELETE，目标是"测试数据库"。
- 事实：`playwright.config.ts` 的 webServer 用固定的 8000/4173 且 `reuseExistingServer:false`；本机 8000 被另一个工作区的常驻后端占用，而那个后端连的是**共享的本机 dev 数据库**（35 张真实图纸）。一旦"临时改 baseURL 指向活服务"跑 e2e，就会把用户图纸清空。
- 结论做法：不要在共享/活数据库上跑 e2e；e2e 只在隔离 DB 的 CI 环境执行。本轮因此**未执行** e2e，改为在 Preview 里手工验证同一批 `data-testid`（面板、findings、trace、项目索引、跨图链接）并把结论如实写进 HANDOFF，而不是写成"e2e 通过"。
- 关键经验：验收脚本的破坏性要先读一遍（`resetDocuments`）再决定能不能跑；"我没跑"必须和"我没验证"区分开——手工在活界面点过并截图，与套件绿灯是两种证据，报告里要写清楚是哪一种。

## 2026-09-18 · 嵌套 subcommand 的 `--database` 必须写在子命令之前（P055-PID-Agent）

- 场景：`pid-agent project-index rebuild --database X` 报 `unrecognized arguments`，而 `pid-agent project-index --database X rebuild` 正常。
- 原因：`--database` 注册在父 parser（与既有 `audit` 命令一致），argparse 要求父级可选参数出现在子命令之前。
- 结论做法：沿用现约定不改 CLI 结构；在脚本/文档示例里固定写成 `pid-agent <group> --database <path> <subcommand>`；CI 用默认路径时不涉及。
- 关键经验：给嵌套命令写文档或写脚本时，先在终端实跑一次顺序，别凭直觉拼参数；这类失败出现在**演练脚本**里（而不是单测里），最容易被误判成代码 bug。

## 2026-09-18 · 稳定身份不能由可变 tag 兼任（推翻上一条身份规则）（P055-PID-Agent）

- 场景：M2 首版把 tag 当工程身份（`valve:hv-101`），测试只验证了“element id 变了但 tag 不变 → id 不变”。第二次验收直接指出这不是 Charter 要的稳定身份：**P-101 改名成 P-201 后，它还是不是同一台设备？** 按 tag 派生身份，答案是否。
- 结论做法：`engineering_id` 改为不可变的代理 id（`sha256("kind|anchor")` → `eq_…/vl_…/inst_…/sg_…/ln_…/jn_…/opc_conn_…`），anchor 优先取图纸里**显式声明**的稳定 id（`identity_basis="declared"`），否则取 element handle（`identity_basis="element"`）；tag 降级为可编辑属性（`tag` / `tag_key`），仍保留 tag 寻址能力；声明 id 冲突报 `IR_IDENTITY_COLLISION`，绝不合并。
- 关键经验：**“稳定”是对什么稳定，必须写清楚并可被测试证伪**。只写“element 变化不影响身份”会漏掉真正的考题（属性变化不影响身份）。判断一个身份方案是否合格，用反例问一句：**业务上允许改的那个字段改了以后，它还是同一个东西吗？** 允许改的字段就不能是主键。
- 连锁技巧：改了身份模型就必须同时处理**旧数据**——本项目把 `IR_BUILDER_VERSION` 从 1 升到 2，让旧索引行自动判 stale（而不是新旧图混用），且旧行在查询入口被跳过而非猜测；缺新 id 的老图纸降级为 element 身份而非报错。

## 2026-09-18 · 硬编码列数的 tab 条：加一个 tab 让整页位移，快照红在别处（P055-PID-Agent）

- 场景：M2 给右侧面板加了第 6 个 tab，本地肉眼看着“只是多一格”，但 CI 上 `blank editor dark theme` 快照相对基线**新增**失败，而报告里很容易写成“预期 UI 变化”。
- 根因：`.right-panel-tabs` 写的是 `grid-template-columns: repeat(5, minmax(0, 1fr))`。第 6 个 tab 自动落到**第二行**（条高 39px → 77px），面板内容整体下移 38px；在 960px 高的页面上这就是几百个像素的位移，足以越过 `maxDiffPixelRatio: 0.015`。
- 定位手法（可复用）：① 下载 CI 失败的 Playwright 产物（`*-expected.png` / `*-actual.png` / `*-diff.png`）；② 自己写 PNG 解码脚本做**逐像素 bbox / 行带 / 列带统计**和 ASCII 热力图——不靠肉眼看图也能判断“改动集中在哪个面板”；③ 在活页面里做 DOM A/B：注入旧 CSS 规则，直接量 `strip.getBoundingClientRect().height` 与下方第一个元素的 `top`，证明“换行 → 内容下移”而不是猜。
- 结论做法：列数改成与 tab 数量无关（`grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr)`），并新增 e2e 守卫断言“所有 tab 同一行且不超出 dock 右边界”——把这类回归从“靠像素快照偶然发现”变成“靠断言必然发现”。
- 关键经验：**像素快照是弱信号（阈值 + 渲染器差异），布局断言是强信号**。UI 里任何 `repeat(N, …)` 的 N 都是未来的 bug。

## 2026-09-18 · 视觉基线是 macOS 渲染器生成的，CI（Linux）长期红——不要用本地重生成去"修"（P055-PID-Agent）

- 场景：CI Browser job 长期 34 passed / 10 failed，其中 9 个是 screenshot。追查发现 `frontend/e2e/visual.spec.ts-snapshots/*.png` 最后一次更新在 UI token 化重构（`86710c0`，早于 T0.5/M2），由 macOS Chromium 渲染，而 CI 在 Linux 上渲染：字体度量不同 → 文字密集页面（左栏、右 dock、顶栏）逐像素判定超阈值。
- 判断依据：同一批 PNG 在基线上就红 8 张，且“本地渲染 == 提交的基线”是 0 像素差；CI 里新增失败的那张，diff 分布与本地一致但整体偏大。即：**基线漂移是既有问题，不是本轮引入**。
- 结论做法：**不从 macOS 重生成基线**（那只是把 macOS 字体烧进基线，复现同一个问题）。正确修法是“在 CI 渲染器里重生成基线”（容器/CI job 内 `npm run test:e2e:update`），作为独立任务；本轮只做归因 + 修真实回归，并在报告里写清“哪个是存量、哪个是新增”。
- 关键经验：跨渲染器的截图基线，必须**在同一个渲染器里生成与校验**；报告里写“快照通过/失败”之前先问“基线是谁生成的”。

## 2026-09-18 · 项目没有 @types/node：vite.config.ts 里用 process 会打断 tsc -b（P055-PID-Agent）

- 场景：为了让第二个 checkout 能改端口，`vite.config.ts` 读了 `process.env.PID_AGENT_API_TARGET` / `PID_AGENT_PREVIEW_PORT`，结果 `npm run build`（`tsc -b`）报 TS2591 `Cannot find name 'process'`。
- 原因：前端只装了 `@types/react` / `@types/react-dom`，没有 `@types/node`；`tsconfig.node.json`（`composite: true`）恰好包含 `vite.config.ts`。
- 结论做法：新增最小环境声明文件（`frontend/node-env.d.ts`，只声明 `process.env` 与 `process.cwd()`）并加入 `tsconfig.node.json` 的 `include`，而不是为一个环境变量引入整套 `@types/node`。
- 附加坑：改完 `tsconfig.*.json` 后仍报旧错时，是 `composite` 的 `*.tsbuildinfo` 增量缓存——**先删 `tsconfig.node.tsbuildinfo` / `tsconfig.app.tsbuildinfo` 再重跑**，否则你会以为改动没生效。
- 关键经验：给构建配置加 Node 全局变量前，先确认项目有没有 Node 类型环境；有 `composite` 增量缓存的构建，改配置后要清 buildinfo 再判断。

## 2026-09-19 · M4 工程校验系统五条可复用教训（P055-PID-Agent）

1. **“fail closed”配置意味着不允许“未知精确规则”后门。** 给 profile 里写错的 `rule_id` 留一个
   “先建个占位规则用着”的逃生口，等于让项目以为自己启用了某条规则而其实没有；拼错的规则必须
   直接抛 `profile_unknown_rule`。未知 **adapter 输出** 是另一回事：finding 不许丢，要标
   `registered=false` / `rule_source="unregistered"` 并让 release gate fail closed，绝不能
   伪装成 `built-in`。
2. **fingerprint 必须包含作用域，而不只是身份。** waiver 的 `object_ids`/`element_ids` 就是语义：
   只把 waiver_id/rule/actor/reason/时间纳入指纹，会让“只覆盖 A 设备”和“覆盖全部设备”的两份批准
   看起来是同一个规则包——审阅者从此无法从指纹看出批准范围变了。
3. **parity 测试必须保留“相关性”。** 把 `(code, severity)` 与引用 id 集合**分开**比较多重集，在
   两个 finding 互换了指向对象时仍然会绿。正确做法是每条 finding 一个关联 tuple
   `(code, severity, sorted(object_ids), sorted(element_ids))`；输出顺序则另用独立测试锁定。
4. **存在过期机制时，墙钟就是输入。** 带 `expires_at` 的 waiver 让校验变成时间相关，若结果不绑定
   评估时刻，同一份输入会出现两个结论且无从解释；`granted_at` 更不能在 profile 加载时用
   `now()` 自动生成——那是凭空制造审批证据，还会让同一份文件每次加载指纹都不同。实现：显式
   `as_of/evaluated_at`、拒绝 naive datetime、拒绝 `expires_at <= granted_at`。
5. **可配置阈值的规则必须由阈值“拥有判决权”。** 如果项目能把质量分门槛从 95 改到 97，canonical
   判定就必须用解析后的 97；否则配置只是展示字段，会出现“配置看起来生效、真实门禁仍认 95”的
   假配置——这比不给配置项更危险。

## 2026-09-18 · e2e 端口要可覆盖，且必须在隔离数据库上跑（P055-PID-Agent）

- 场景：本机 8000 端口被另一个工作区的常驻后端占用（连着共享的 35 张真实图纸 dev DB），而 `playwright.config.ts` 的 webServer 固定 8000/4173 + `reuseExistingServer:false`，于是“想跑 e2e”就变成“要么清活库、要么跑不了”。
- 结论做法：把端口改为环境变量可覆盖（`PID_AGENT_E2E_API_PORT` / `PID_AGENT_E2E_PREVIEW_PORT`），`vite.config.ts` 的 preview proxy 读 `PID_AGENT_API_TARGET`，`fixtures.ts` 的直连 API 根地址由同一组变量推导；CI 不传变量即用默认值。数据库仍然由 config 注入到 `test-results/*.db`，所以永远不会碰活库。
- 关键经验：**验收脚本的破坏性要写进它的接口设计里**——只要“目标库/端口”是硬编码的，下一个人就会在错误的库上跑它；让端口和库路径可参数化，是把“别这么做”变成“做不到”的唯一办法。
