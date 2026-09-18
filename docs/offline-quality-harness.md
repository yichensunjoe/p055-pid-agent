# P&ID-Agent 离线质量 Harness

这里的 harness 指可重复运行的工程“测试台/验收夹具”。它不调用真实模型，也不需要 API
Key，用于在图例库或语义事务代码发生变化后，快速判断基础契约是否仍然成立。

## 运行

先安装项目，然后在仓库根目录执行：

```bash
source .venv/bin/activate
pid-agent quality-harness
```

同时验收单位或项目图例：

```bash
pid-agent quality-harness \
  --symbol-path /absolute/path/company-symbols \
  --symbol-path /absolute/path/project-symbols.json \
  --output reports/quality-harness.json
```

`PID_AGENT_SYMBOL_PATHS` 中的图例仍会加载；重复使用 `--symbol-path` 可追加本次验收路径。
后加载的相同 `key` 按正常产品规则覆盖前面的定义。

退出码：

- `0`：全部离线检查通过；
- `2`：至少一个检查失败，或外部图例 JSON/Schema 无法加载。

报告是稳定的 JSON，不包含 API Key、用户图纸或本机数据库内容。测试只使用自动删除的临时
SQLite 数据库，不会修改正在使用的 P&ID。

外部文件包含错误 JSON、错误 `symbols` 结构、非法 library metadata、Schema 校验错误或
同一文件内重复图例 `key` 时，命令同样输出 `pid-agent.quality-harness` JSON 报告，不输出
Python traceback。错误位于 `symbol_catalog_load` case，并带稳定的 finding code。一个 JSON
文件内重复 `key` 通常是复制错误，因此会失败；不同文件或内置/单位/项目层之间相同 `key`
仍按加载顺序合法覆盖。

## 六个检查

### 1. `symbol_catalog_integrity`

对当前实际加载的全部图例动态检查，不依赖固定数量或固定分类：

- `key`、端口 ID 是否为稳定的小写英文标识；
- 名称、分类、工程说明是否齐全；
- 端口是否重名、越过局部坐标范围或缺少介质/中文名称；
- `line`、`polyline`、`rect`、`circle`、`path`、`text` 的必需字段是否合法；
- 全部图例能否一起完成后端 SVG 冒烟渲染。

无端口的纯标注或设备附件图例是合法的。新增图例会自动进入下一次扫描，不需要修改测试中的
预期总数。

### 2. `atomic_topology_transaction`

从目录中动态选择一个具有中心线进出口的直通图例和一个可连接图例，在临时数据库中用一次
原子事务建立：

```text
源设备 ── junction ── 目标设备
              │
            支路图例
```

检查 revision 只增加一次、七个图元全部持久化、三个 connector 都绑定真实端口、junction
度数为 3、管线保持正交，并再次读取 scene summary。它可发现图例端口改名后事务失效、
连接退化成自由线段、非原子写入或存储/摘要回归。

### 3. `semantic_agent_output_contract`

构造一份与模型返回格式完全相同、但内容固定的 `SemanticAgentPlan` JSON，依次包含：

- `connect_ports`；
- `instrument_tap`；
- 固定 element/connector ID、介质、管径和流向。

计划必须经过真实 Pydantic schema、语义编译器、完整文档验证和原子应用，再检查主管拆分、
真实 junction、仪表支路和正交连接。随后再提交一份带虚构 `port_id` 的已知错误计划，确认
它以 `unknown_port` 被拒绝，并且 document revision 不变化。

这能验证“模型输出之后”的确定性安全边界，但不会证明某个真实模型能稳定理解自然语言。

### 4. `drafting_quality_contract`

这个 case 专门验证“图连对了但画得难看”的失败模式。它先让生产语义编译器连接一组已对齐
的真实端口，要求确定性路由得到零折点、100 分的图面；再注入一条只有 5 单位高差的阶梯形
管线，要求质量门禁稳定拒绝 `MICRO_SEGMENT` 和 `UNNECESSARY_BEND`。

同一套质量分析还会检查：

- 斜线、微小线段、不必要折点和超过三折的管线；
- 端口方向、端口外法线、入口/出口朝向及主流程反向；
- 图例重叠、管线穿设备、图元或管线越出画布；
- 几何相交却没有跨线桥的管线；
- 重复标签，以及文字与文字、图例或管线的重叠。

完整新图必须没有阻断项且达到 95 分；不达标会以结构化 issue 反馈给模型重规划。局部编辑仍
保留宽容编译，避免一处历史遗留排版问题阻止无关修改。

### 5. `engineering_graph_contract`

构造一份带位号、管线、跨图连接器与重复位号的图纸，验证 M2 的工程语义图契约：

- 工程对象带**不可变代理 id**（`eq_…` / `ln_…` / `opc_conn_…`）与 `identity_basis`；
- 管线按 tag + 介质 + 口径聚合，flow-aware trace 只在同一边类（工艺/信号）内遍历；
- 重复位号报 `IR_TAG_DUPLICATED` 而不是合并对象；
- project index 的 cheap/verified 两级新鲜度、计数与跨图连接可查询。

它保证的是“图变成事实”的那一层：**派生层永不写文档，且同一份文档永远推出同一张语义图**。

### 6. `deterministic_drafting_contract`

M3 确定性整理引擎的离线黄金契约。它先构造一张**故意脏**的图纸（重叠设备、无意义绕行管线、
压在符号上的标签、悬空连接点、一个被锁定的元素、一块声明图例、以及一条穿图例的管线），
然后要求引擎同时满足：

- **只读**：preview 后 document revision 不变；
- **可复现**：两次相同请求得到同一个 `transaction_digest`；
- **单调**：`regressions == []`，且分数不下降；
- **锁定优先**：锁定元素没有被移动或编辑，且锁定来源被如实记录；
- **拓扑不变**：管线端点绑定、元素集合都与应用前一致；
- **保留区**：穿过图例的管线被改道、**压在图例上的符号被移出**，保留区侵入数降为 0；
- **收敛**：把结果再喂回引擎即 `settled`；
- **如实**：残留项只能是引擎不允许修的语义缺陷（重复标签），门禁必须拒绝签字。

最后一条是刻意设计的：这个契约**不**要求“门禁通过”，而要求“几何残留归零 + 剩下的问题
必须被如实拒绝”。草稿引擎不得为了让自己的分数好看而改写标签文字或隐藏缺陷。

## 与其他验收的关系

| 层次 | 命令 | 是否联网 | 主要回答的问题 |
|---|---|---:|---|
| 离线质量 Harness | `pid-agent quality-harness` | 否 | 图例、存储、端口拓扑、Agent 事务、工程语义图与确定性绘图/整理契约是否完好 |
| 浏览器验收 | `cd frontend && npm run test:e2e` | 否 | 人工编辑、刷新持久化、视觉和交互是否正常 |
| 真实模型矩阵 | `pid-agent model-matrix ...` | 是或本地模型 | 指定模型能否多次从自然语言生成并修复正确事务 |
| 大图基准 | `PYTHONPATH=backend python backend/benchmarks/benchmark_large_documents.py` | 否 | 500–5000 图元的导出耗时和内存是否退化 |

日常改图例或事务代码时先运行离线 Harness；合并前继续运行 pytest、前端测试和 Playwright；
准备宣称某个模型“可用”时，仍必须运行至少三次重复的真实 `model-matrix`。

## 如果 “harness” 指线束

电气 wire harness 与本文件的测试 harness 不是同一概念。P&ID-Agent 的图例 Schema 已允许
使用 `electrical`、`signal` 等端口介质，因此可以增加接线端子、线束边界和信号连接图例；
但线号、芯线、连接器针脚、分支束和线束表属于独立的数据语义。若目标是专业线束设计，应先
扩展结构化模型和报表规则，而不是只画一组看似相连的线。
