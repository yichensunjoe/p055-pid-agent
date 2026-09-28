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
