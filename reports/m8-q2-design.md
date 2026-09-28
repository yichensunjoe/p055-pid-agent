# M8-Q2 Catalogue Correction / Expansion · 设计（报 Gate，实现未授权）

> 基线：m8-q1r @ 3584708（= main@1afeccab + Q1R）；证据除注明外均出自 17-step frozen rerun（b17ab08 证据卷）与 2026-09-28 下午的定向复现（scratch，已清场）。
> 范围纪律：G4（router）、HOLDOUT-1「换成」语法、system declaration 支持均**不属于**本设计；catalogue/frozen corpus 在本次设计中只动数据与词表，不改 frozen corpus 句子。

## Q2-1 TT-101「温度变送器」—— 真相是 hint 中英错配，不是缺符号（correction，非 expansion）

**Exact evidence**（DEV-4 句一 422 catalog_gap 全量体）：
- `requested_type=温度变送器, requested_tag=TT-101, candidates=0`，但 `available_alternatives` 含 `temperature_indicator`（温度指示仪，category=仪表）、`temperature_element`（温度检测元件，category=仪表）。
- catalogue 数据实证：`temperature_indicator`/`pressure_indicator`/`flow_indicator` 的 category=「仪表」，`level_gauge`/`temperature_element` 同；**符号库存不缺温度仪表**。
- 机制：`device_phrases.SYMBOL_HINTS` 中 `(("仪表","变送","transmitter",...), ("instrument","transmitter","sensor"))` —— 触发词含中文，**hint 元组纯英文**；`candidate_symbols()` 用 `any(hint in haystack)` 过滤，haystack=「key + 中文名 + 中文category」，英文 hint 永远命中不了中文数据 → 0 候选 → 误报 catalogue gap。
- 对照：LIT-101「液位计」与 PIT-101「压力表」因**不含任何触发词**（wanted 为空 → 不过滤 → 24 候选走 TypeSafe 判定）而"侥幸成功"——同一句话里三种仪表三条不同路径，属测量噪声。

**修法（correction）**：给 SYMBOL_HINTS 的 hint 元组补中文字段（如 ("instrument","transmitter","sensor","仪表","变送","指示仪","检测元件")），或按 category 直连。**改动面**：`backend/agentcad/device_phrases.py`（词表代码，非 catalogue 数据文件）。**digest 影响**：无——hints 不进 layout identity/symbol geometry digest（m7_symbol_geometry 的 fact 无 hints/selectors 字段；M7-Q2 已把 port name 排除在 identity projection 外）。**测试矩阵**：①「温度变送器 TT-101」→ candidates 非空且全为仪表类；②「液位计/压力表/流量计」行为不变（负守卫）；③catalogue-gap receipt 只在真无候选时触发（构造不存在的设备词）；④DEV-4 句一重跑期望 3 仪表全 resolved、complete。

## Q2-2 G3 fractionation 词表 —— 拆成两半：词表 correction + 一个 planner 代码缺陷（需路由裁定）

**Exact evidence**（DEV-6 句二 422 全量体，role=source）：
- `source_requirement=「把 T-101 的塔顶气相出口接到 E-101 的工艺入口」`，`element_tag=T-101`，但 `selector.raw=「工艺出口」`——**该 requirement 文本里不存在「工艺出口」四字**（它只属于第二条 requirement）。selector 提取张冠李戴，T-101 端口候选（bottom/overhead/side_draw）拿着错的 selector 当然 no_match；「回流入口」从未被评估。
- 附带观察：该 receipt 的 T-101 候选只有 3 口（feed/reflux 缺席）——疑似已占用端口被排除（cn_1 占用 feed）；待实现阶段确认这是契约还是巧合。

**拆半**：
- (a) correction 部分（属 Q2）：按修订版 T6 契约（port.name 必须 resolve self、selectors[] 每个 resolve self），为 fractionation_column 各端口补全 name 自解析与缺失别名（回流入口/侧线采出口等）。改动面：`m7_port_selectors.py` 的 SEMANTIC_SURFACE_TOKENS / 别名表（**该文件当前在 Q1R 禁改名单，Q2 需明确解禁**）。digest 影响：selectors 不进 symbol geometry fact（已核 m7_symbol_geometry 无 selectors 字段）→ 无 identity digest 变更；若解冻后证实 selector 数据进 digest，则按 COORDINATE_QUANTUM_CHANGE_REQUIRES_VERSION_BUMP 的既有机制 bump 并更新 frozen fixture。
- (b) planner 缺陷（**不属于 catalogue Q2**）：selector 提取在多条 requirement 间串台。这是 m7_text_planner/requirement 解析代码缺陷，需您裁定路由（单独小批还是并入 Q2 实现白名单）。

**测试矩阵**：①端口自解析 contract（T6 修订版：∀port name→self、∀alias→self、多命中→显式 ambiguity）；②DEV-6 句二在 (b) 修复后：塔顶气相出口/工艺入口/回流入口逐口 selected；③负守卫：不存在的 selector 仍 no_match 且 receipt 携带正确 requirement 文本。

## Q2-3 G6 shell_in「塔顶/顶部管口」—— 建议直接 CLOSE，不是数据污染

**Exact evidence**：m7_port_selectors.py:70 的**侧位通用词表**（"顶部管口"/"塔顶"→(side,top)）按端口几何方位派生别名——shell_in 在顶侧(top)即自动获得这些别名。这是派生行为（by design），不是 catalogue 数据里混入了塔词汇；旧观察把派生别名误读为数据污染。按修订版 T6，「塔顶」匹配多个 top 侧端口时应显式 ambiguity 而非默认选择——这是 matcher 行为契约，已在 Q1 的 contract-test GO 里冻结，无需 catalogue 改动。

**建议**：G6 = CLOSE（无 catalogue 变更）；若您要求按符号收敛别名（换热器壳程不该吃塔词汇），那是一个**新的 per-symbol 别名数据面**设计（改动面扩到 catalogue schema + digest 机制），请单独裁定是否值得。

## 汇总请求

1. 批准 Q2-1 correction（device_phrases 词表补中文字段）+ 上述测试矩阵；
2. Q2-2(a) 明确解禁 m7_port_selectors.py 的词表数据部分；Q2-2(b) selector 提取缺陷请路由；
3. Q2-3 G6 请确认 CLOSE；
4. Q2 实现白名单与 digest bump 机制在获批后随实现计划一并提交。

## Q2-1R 设计修订（visibility + language-match，按 Gate 分类重报；暂不动代码）

**Exact evidence 补全**：
1. `temperature_transmitter` 存在于 standard_symbols.json（name=温度变送器，category=仪表，ports=process 过程接口(in)/signal 信号接口(out)），但被 `HIDDEN_BUILTIN_SYMBOL_KEYS`（symbols.py:14-21）抑制：`registry.list()` 不含它 → 候选池为零 → DEV-4 误报 catalogue gap。
2. 隐藏历史：`117a8f7`（2026-07-27「harden web agent P&ID workflow」）一次性隐藏 pressure/temperature/flow/level 四个变送器；`8d82f75`（2026-08-06）把 **pressure_transmitter 单拎解禁**——frozenset 删一行，无其他仪式 = 直接先例。代码中未记录继续隐藏 TT 的约束；唯一的"约束"是审计契约把 hidden 视为 **"withheld, one-line fix behind this verdict"**（test_m7_proposal_accountability.py:913 用 flow_transmitter 作 hidden 判定的范例）——即代码库预期解禁就是修法本身。
3. 语言匹配复核：**TT 解禁后现有英文 hint 已足够精确**——haystack「temperature_transmitter 温度变送器 仪表」含 "transmitter"（key 自带），而 temperature_indicator/element 的 haystack 均不含 → 候选恰好 [temperature_transmitter]，lookalake 进不来。中文 hint 补丁非必要（ Gate 说"可以补但不是靠宽泛仪表拉 lookalikes"——现状已满足）。

**设计（修订后）**：
- 只解禁 **temperature_transmitter** 一项（frozenset 删一行，沿 8d82f75 先例）；**FT/LT 不动**（无独立证据；且审计测试需要至少一个 hidden 范例，FT 正是该范例）。
- 同步改 `test_symbol_library.py` 的 `HIDDEN_BUILTIN_KEYS` 期望集（去 TT）；审计范例测试不动（用 FT）。
- **identity 影响如实记录**：symbol 数据不动 → 既有图纸的 geometry/identity digest 不变；但 visible catalogue/candidate surface 变化（list() 多一项）→ 未来含 TT 的 judgment 候选集变化。这正是 correction 的目的，非副作用。
- **风险与验证项**：TT 的 process(in)/signal(out) 端口模型与「给 V-101 添加」宿主挂接语义的端到端配合（DEV-4 重跑验证：TT-101 挂到 V-101、LIT/PIT 行为不变）；若宿主挂接受阻，回报 Gate 再定（可能涉及 instrument attachment 语义，不属于本 correction）。
- 测试矩阵：①DEV-4 句一重跑三仪表全 resolved 且 TT-101→temperature_transmitter（断言精确 key，非 indicator/element）；②HIDDEN_BUILTIN_KEYS 期望集更新；③FT/LT 仍 hidden（负守卫）；④既有 symbol_library/audit 测试全绿。
- 改动面：`backend/agentcad/symbols.py`（一行）+ `backend/tests/test_symbol_library.py`（期望集）。digest 无 bump。

## M8-Q2R2 设计：connect 子句内嵌目标设备声明（DESIGN GO 已批；实现待签）

**Exact evidence**（17 步后测 + 定向复现，m8-q2a 代码）：「把 T-101 的侧线采出口接到一个缓冲罐 V-102」→ skipped「没有得到判断，已跳过」+「V-102 没有任何设备声明」+ undelivered；无 port receipt。机制：`split_clauses()` 遇 connect 动词把整句判为 connect；editor/planner 的新设备只来自 add 子句 → 内嵌的「一个缓冲罐 V-102」从未进入 delta entity 路径。

**冻结规则（按 Gate 边界锁窄）**：
1. 仅当 connect 子句的某一端明确为「量词 + 设备短语 + 唯一新 tag」时，把该内嵌设备登记为 delta entity（phrase=设备短语、tag=新 tag、source_clause=**原始整句原文**——不得伪造成用户说过「添加一个缓冲罐 V-102」），并保留原 connection requirement。
2. 原 connection 的该端点绑定到新实体；semantic ledger 产出「新实体 V-102/缓冲罐 + 原连接 T-101→V-102」两条账。
3. 反自动创建设备：「接到 V-102」式（无设备短语）不自动创建；无明确 tag 不创建；已存在 tag 不重复添加。
4. 不做通用逗号/「一个」字符串拆分；多设备内嵌（ enumeration 形态）不处理（超出本批）。
5. HOLDOUT 不用于驱动规则扩张（本设计只用 DEV 证据）。
6. 解析放在**共享 planner read 层**（`split_clauses` 之后、分类结果之上的一层窄展开），editor 经 `self.read()` 自动继承——不给 DEV-6 在 m7_text_edit.py 写特判。

**形状判定（与 G2 同风格的保守守卫）**：端片段匹配 `^(一(台|个|只|款))?<设备短语> <TAG>$`（TAG_PATTERN 唯一且不在已知 tag 集）；含第二量词或第二个 tag 则不展开；含 remove/connect 动词片段不展开（防误拆）。设备短语须命中 SYMBOL_HINTS（含 exact 表）——无 hint 的片段不展开（走原有 undelivered 路径，防 phantom）。

**测试矩阵**：①DEV-6 句三全链：s3 complete、V-102 实体落 buffer_tank、连接 cn_4 T-101(side_draw)→V-102、source_clause 保留原句；②负守卫：「接到 V-102」无短语不创建；无 tag 不创建；已存在 tag 不重复；含两个 tag 不展开；③G2 枚举回归 + Q2R1 remap 回归不破坏；④端到端画布验收（编辑后重画对账通过）。

**改动面（实现获批后）**：`backend/agentcad/device_phrases.py`（共享展开层，read() 调用点传参）或 `m7_text_planner.read()` + `m7_text_edit` 继承点；**白名单外不动** m7_port_selectors.py / catalogue / corpus。

## M8-Q2R3 设计：instrument attachment 语义（DESIGN GO；实现 BLOCKED pending design）

**问题陈述**（exact evidence）：DEV-4 句一在 TT 解禁后，三仪表全部在 planner 层精确解析，但 layout 第一步拒入：`routing needs resolved endpoint bindings: without them it would have to guess ports`。机制：M7 `DiagramSpec` 的 instrument 实体只有 kind/phrase/symbol，**没有任何"挂接到谁"的关系**；`PlannedEntity` 同样没有 host 字段；`read()` 把「给 V-101 添加」的宿主部分当成普通修饰丢弃。endpoint binder 的契约是 routing 前必须解析到唯一真实 port——所以不能靠"找最近端口/接罐顶"糊。

**调研结论：不复用 instrument_tap 操作本体，复用其物化词汇。** `InstrumentTapOperation`（agent_semantic_models.py:129）是**起草层操作**：要求已存在的 `main_connector_id + junction_point`（几何量），在 spec 无坐标阶段不可用。但它的下游物化词汇（junction / root valve / instrument 三件套）是成熟语义，Q2R3 在 layout 物化时复用这些概念，不重造。

**设计答案（对应您的七个问题）**：
1. **DiagramSpec 增加正式 attachment 关系**：`DiagramEntity` 新增 `host_engineering_id: str = ""`（instrument 专用；equipment 恒空，validator 断言）。semantic-first：关系活在语义 spec 里，不在几何里。
2. **host identity 保存**：`host_engineering_id` 存工程 id（el_V_101），不存 tag（tag 可改，id 稳定）；`spec.problems()` 增加"instrument 的 host 必须已声明"一致性；planner 在 read() 解析「给 <TAG> 添加<仪表>」时填该字段（枚举分解后的子句已带宿主前缀，G2 产物直接可用）。
3. **process / signal 接口语义**：process 口 = 物理取压/测量点（接到宿主）；signal 口 = 信号去向——**单句植物料图不连接 signal**（不接 DCS/不猜），该口保持未用是合法终态，不是 gap。TT/LIT/PIT/FT 的 process 绑定语义由仪表符号自己的 port 模型承载（TT 现有 process(in)/signal(out) 即此）。
4. **宿主无可确定性挂接点时 → structured receipt**，不推导：receipt 命名 host tag + instrument tag + 原因（如「V-101 没有可挂接的仪表口」）。**严禁**把宿主的 process inlet/outlet 静默当 instrumentation tap——作为显式 invariant 进 validator + 负测试（挂接关系只能由 attachment 字段建立，不能由端口方向巧合建立）。
5. 挂接点（tap 落在宿主哪个几何位置）由 layout 引擎**确定性推导**（规则进 m7 契约，如"罐类液位计→顶侧法兰位；压力/温度→顶部管口"），推导规则本身是数据/契约，不猜——但规则表的制定属实现批次内容，设计阶段只锁定"确定性规则 + 规则外 receipt"。
6. **redraw / digest / stored semantic source**：attachment 字段是语义 spec 的一部分 → 进 spec_digest（自然变化，无手工 bump）；identity projection 新增 host_engineering_id（对 instrument 行）；stored semantic source 随 revision 保存；redraw 走既有 governed 链路（edit → 新 spec → 重画），关系不丢。
7. **targeted cases**：TT-101→V-101（温度变送器挂罐）、LIT-101→V-101（液位计挂罐）、PIT-101→V-101（压力表挂罐）各一，端到端 commit + export readback PASS。

**测试矩阵**：①三 targeted case 端到端 complete；②attachment 一致性（host 未声明 → 422 receipt）；③负守卫：process 口不得被当 tap（构造无 attachment 字段的 instrument spec 断言 validator 拒）；④signal 口未连接不产生 gap/undelivered；⑤edit 第二句再加仪表 → 关系进 revision 2，redraw 对账 PASS；⑥frozen corpus DEV-4 重跑三仪表 complete。

**改动面（实现获批后申报）**：m7_diagram_spec.py（字段+validator+problems）、m7_text_planner（read 解析宿主填字段）、m7 绑定/物化层（tap 规则 + 三件套物化）、digest projection、对应测试。**禁改**：m7_port_selectors.py 词表、catalogue、frozen corpus。
