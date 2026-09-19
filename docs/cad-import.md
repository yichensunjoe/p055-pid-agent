# CAD 导入（DWG / DXF intake）

本文件描述 Charter `§14（图纸接入）/ §7（唯一受治理写通道）/ §19（审计）/ §21.6（可复核）`
与 P0（**结构启发式不得创造工程语义**）在“把外部 CAD 图纸拿进项目”这件事上的落地。

一句话定位：**导入只复现图面（几何、图层、文字、块出处），不推断工艺**——管线、设备、
仪表、连接关系都不猜；猜不出来的东西逐条列在导入报告里，而不是静默丢掉。

## 1. 目标与非目标

| | 内容 |
|---|---|
| **目标** | 把 DWG / DXF 的**可绘制内容**变成原生文档：线、折线、圆、采样后的弧/椭圆、实心填充、文字、图层与颜色；结果带 revision、历史、审计、undo，并给出一份“哪里不像原图”的报告 |
| **非目标** | 识别符号语义（`gate_valve`、`ball_valve`…）、把折线连成管线、把块实例变成设备、还原标注尺寸/图框语义、生成声明式稳定标识（`equipment_id`/`line_id`） |

非目标是刻意的：符号库映射表必须人工确认，否则就是 P0 明令禁止的“结构启发式创造工程
语义”。因此导入结果是一份**几何复现**，工程图谱里它是图元，而不是设备与管线。

## 2. 三条路线，靠“保真度”而不是“方便”排序

| 路线 | 实现 | 许可 | 说明 |
|---|---|---|---|
| **DXF** | `cad_dxf.py`，**本项目自研**的 group-code 读取器（约 900 行，零外部依赖） | 自有 | ASCII/二进制 DXF 都能读；块展开、bulge、镜像/嵌套/阵列插入、HATCH、MTEXT 全部本地处理。用户手上已有 DXF 时这是最短路径 |
| **DWG → AutoCAD Core Console**（机器上装了 AutoCAD 时首选） | `cad_convert.py`：调用 AutoCAD 自带的**无界面核心引擎** `accoreconsole`（macOS 上位于 `AutoCAD <年份>.app/Contents/Helpers/AcCoreConsole.app`），脚本 `DXFOUT` 出 DXF，再交给自研读取器 | 用用户自己安装的 AutoCAD，按其许可运行；**不打包、不再分发、不链接** | 官方引擎，保真度最高：真实图纸实测 **9757** 个图元、**0** 个块定义缺失（见 §6） |
| **DWG → 对象流** | `cad_convert.py` + `cad_dwg.py`：调用外部 `dwgread -O JSON`，再把对象流展开成几何 | LibreDWG 为 **GPL-3.0**，仅以 **subprocess** 调用 | 无 AutoCAD 时的最佳免费路线；能保住 LibreDWG 自己 DXF 写出器会丢的**动态块**内容 |
| **DWG → DXF**（兜底） | `cad_convert.py`：外部 `dwg2dxf`（或 ODA File Converter）产出 DXF | 同上 / ODA 为免费闭源 | 目标版本 r12/r2000/r2007/r2013 都试过，动态块仍会退化 |

**外部工具的打包边界（事实口径）**：AutoCAD / LibreDWG / ODA 都只作为**外部可执行工具**被
`subprocess` 调用：本项目**不打包、不复制、不 vendor、不链接**任何一个，也不随发行版分发任何第三方
二进制；每个工具仅在操作者**已经自行安装**它时才被使用，其安装与使用受到**该工具自身的许可条款**
约束——那套条款是否适合某个部署，是操作者的判断，本项目不为操作者下这个结论。FreeCAD 的
`importDWG.py` 不采用的原因是纯工程性的：它本身就是“`subprocess dwg2dxf` 的一层薄封装”（LGPL 代码），
搬过来不会多出任何能力。读 DXF、展开对象流、生成文档这三件事的代码全部在本仓库内，属自有实现。

转换器的发现与调用都留证据：`capabilities()` 报告每个候选的 `executable` / `available` /
`version` / `evidence`，`convert_dwg()` 把**每一次尝试（含失败与超时）**记进
`CadConversionAttempt`，因为“dwg2dxf 超时了”和“这张图没有几何”是两件不同的事。

**`evidence` 的准确含义**：`verified` 指**该工具这一族**在参考图纸/参考语料上跑过并达到表中保真度，
**不等于**这台机器上装的**这个版本**已被认证；`unverified` 表示本项目还没有在参考图纸上跑过它。

**路径披露决定（shared / local 模式一致）**：能力清单与导入报告都是**任意客户端可读**的，因此它们
不暴露本机路径——`executable` 只给可执行文件**基名**，命令行里的私有临时目录被替换为 `<workdir>`
标记（`public_command()` / `public_executable()`）。精确路径只在本机诊断与操作者自己的 CLI 场景下存在，
不进入对外契约。**失败即失败**：转换器返回非零退出码时，即使 stdout 能解析成对象流、目标目录里也有
非空 DXF，该次尝试仍记为 `failed` 并继续尝试下一个候选（`acceptable_exit_codes` 默认 `(0,)`，只有
带测试与证据的显式例外才可覆盖）。

## 3. 接口面

| 接口 | 作用 | 性质 |
|---|---|---|
| `GET /api/v2/imports/cad/capabilities` | 本机能不能解 DWG、用什么解、上限多大 | 只读（`test_capabilities_route_writes_nothing` 直接断言它不写库） |
| `POST /api/v2/imports/cad/plan` | 解码 + 翻译 + 返回完整报告，**不写任何东西** | 登记为 **read**；前端“仅解析图纸”按钮走它 |
| `POST /api/v2/imports/cad` | 真正导入，创建**一个新文档** | 登记为 `project_metadata` 写、tool `import_cad_drawing`、`audited=True` |
| MCP `import_cad_drawing` | 从服务器主机上的路径导入 | tool `import_cad_drawing`，`audited=True` |
| CLI `pid-agent import-cad <file>` | 同样的导入，输出人类可读/JSON 报告 | 与 REST 共用 `CadImporter` |
| 前端 | 左侧「导入 DWG/DXF」与「仅解析图纸」+ 导入报告面板 | 只读消费；报告逐条显示“未能复现” |

请求体是**原始字节**而不是 multipart：本项目没有 multipart 依赖，浏览器可以直接把 `File`
当 `Blob` 发出去，后端 `await request.body()` 读取。`/api/v2/imports/*` 本来就有导入体积上限
（默认 25 MB，`max_source_bytes`），超限返回 413。

查询参数（全部可选）：`filename`、`name`、`frame=x0,y0,x1,y1`、`layers=a,b`、
`include_text`、`fills=solid|skip`、`curve_segments`、`stroke_width`、`unit_scale`、
`preserve_colors`、`max_elements`。

> `chunk_size` 已移除：导入不再是“分批事务”，而是一次逻辑受治理变更（见 §5）。

## 4. 导入报告的契约

报告（`CadImportReport`）是这次导入的**唯一诚实凭据**，字段如下：

| 字段 | 含义 |
|---|---|
| `source` | 文件名、格式（`dxf`/`dwg`）、格式细节（`ASCII`/`AC1024`…）、字节数、**SHA-256**、编码、使用的转换器、转换器版本与**归一化后的**命令行 |
| `counts` | 图层/线/折线/圆/文字/填充/原生图元/源图元/被跳过数 |
| `source_bounds` / `imported_bounds` / `frame` / `canvas` | 源坐标范围、导入后的范围、裁剪窗口、画布尺寸 |
| `layers` | 保留下来的图层名（顺序稳定） |
| `issues` | **每一类未能复现的东西**（code + 人类可读 message + 数量 + 明细） |
| `operations` / `logical_mutations` / `revisions` / `duration_ms` | 落地细节：操作数、逻辑受治理变更数（真导入 1、dry run 0）、revision 数（1）、耗时 |
| `warnings` | 不阻断但需要人知道的注意事项 |

`issues` 的 code 是稳定契约（前端与测试引用同一批字符串）：

| code | 含义 |
|---|---|
| `CAD_UNSUPPORTED_ENTITIES` | 未表达的实体类型（带类型计数明细） |
| `CAD_MISSING_BLOCK_DEFINITION` | INSERT 指向不存在的块定义——**不猜**，只记账 |
| `CAD_BLOCK_DEPTH_LIMIT` | 嵌套块展开到深度上限后停止 |
| `CAD_HATCH_BOUNDARY_UNREADABLE` / `CAD_HATCH_ISLANDS_OUTLINED` / `CAD_PATTERN_HATCH_SKIPPED` | 填充：边界不可读 / 带岛屿改用轮廓线 / 图案填充跳过 |
| `CAD_SPLINE_EDGE_SKIPPED` | 样条边界或样条曲线未表达 |
| `CAD_PAPER_SPACE_SKIPPED` | 布局空间（图纸空间）内容未导入 |
| `CAD_INVISIBLE_ENTITIES_SKIPPED` | 不可见实体（DWG 对象流中的 invisible 位）跳过 |
| `CAD_TEXT_ROTATION_IGNORED` | 旋转文字的锚点保留、旋转被忽略 |
| `CAD_DEGENERATE_GEOMETRY` | 导入后没有尺寸的形状（零长线等）被丢弃 |
| `CAD_TEXT_HEIGHT_CLAMPED` | 字高超过编辑器上限被截断（位置不变） |
| `CAD_CIRCLE_APPROXIMATED` | 位于**非均匀缩放**块参照里的圆实际是椭圆，已按变换后的曲线采样为闭合折线 |
| `CAD_PRIMITIVES_NOT_IMPORTED` | 被导入选项排除（图层过滤 / 关文字 / 关填充 / 帧外），明细按原因分类 |

## 5. 落地方式：一次导入 = 一次逻辑受治理变更

导入**不是**一条私有写路径，也**不是**“一串事务”：

1. `DocumentService.create_document_with_operations()` 创建**一个新文档**
   （`name` 默认 `<文件名> (CAD 导入)`），并把**整张图的图元操作一次性**应用、校验、落盘；
2. 因此一次导入 = **一个 revision、一条历史记录、一条审计记录、一次 undo**。
   `undo` 一次就把整张导入的图撤掉（回到刚创建的空文档），`redo` 一次再全部装回。
   **没有任何中间态会被持久化**：几何只在内存里逐个操作应用，全部成功且文档通过校验后才写入。
   这里**没有分块写路径**——操作序列在内存里顺序落地，完成并通过校验的文档一次性持久化。
3. 文档 `metadata` 记录 `cad_import`（文件名、格式、SHA-256、转换器、帧、统计）；**元素级**的出处键是
   `cad_source`（文件名）+ `cad_block` / `cad_handle`。
4. **源指纹同时写进审计记录**（Charter §19）：`revision.created` 的 evidence 里带
   `cad_import.source_sha256` / `source_format` / `converter` / `converter_version`，由服务端从**实际
   解码的字节**算出，不取自请求字段。只写文档 metadata 是不够的——metadata 可以被后续修改，
   审计记录才是导入契约的凭据。
5. **它不能修改、重新版本化或删除任何既有文档**——`surface_contract.py` 把这条写成
   `DRAFT_EDIT_EXCEPTIONS["import_cad_drawing"]` 的显式理由，并由测试断言。

> 为什么改：早期实现是“先建文档，再按 1000 操作/批提交事务”。它有两个真实缺陷——
> 中途失败会留下**看起来正常**的半张图；而 `undo()` 只弹一层栈，大图一次 undo 只能撤最后一批。
> 用户语义上的“导入”必须是一件事，原子性与撤销语义就必须定义在**用户级操作边界**上。

## 6. 三条 DWG 路线的实测对比（同一张真实图纸）

用户提供的真实图纸 `气路系统总图.dwg`（917 KB，AC1032，9283 个实体）用我们这个读取器
（`cad_dxf.read_dxf`）分别解析三条路线产出的内容：

| 路线 | 原生图元 | 丢失的块定义 | 关键问题 |
|---|---|---|---|
| **AutoCAD Core Console → DXF** | **9757** | **0** | 无（官方引擎，按 AutoCAD 当前显示的动态块状态展开） |
| `dwgread -O JSON` 对象流 | 9242 | 0 | 需要 GPL 工具；文字/旋转等细节靠自己重建 |
| `dwg2dxf` → DXF（r2000/r2007/r2013/r12 都试过） | 6523 | **157** | `PDS2D-6Q1C15`（140 个阀门实例）、增压泵（12）、止回阀（3）、风机（2）的**块定义为空**，图面上最显眼的阀门符号全部消失 |

另外两条被排除的路线：`dwg2SVG` 虽然解析出了块内容，但把内容画到图纸范围之外
（`getBBox()` 延伸到 −863467），不能当参考；FreeCAD 的 `importDWG.py` 等价于
`subprocess dwg2dxf`，所以与第三行同病。

因此选路顺序写进 `CONVERTER_TEMPLATES`：**AutoCAD 核心引擎 → LibreDWG 对象流 → ODA →
LibreDWG DXF**，并且只尝试**已经安装**的工具；报告如实标注最后用的是哪条（`source.converter`
与 `converter_command`），`evidence` 区分 `verified`（在真实图纸上跑过）与 `unverified`。

**AutoCAD 核心引擎的两个使用要点**（已固化为代码与测试）：

1. 它不在 `PATH` 上，且 macOS 上叫 `AcCoreConsole`（在 `AutoCAD <年份>.app` 的
   `Contents/Helpers/` 里），所以按 `EXECUTABLE_GLOBS["autocad-core-console"]` 通配查找；
   找不到就是找不到，不会伪造路径。
2. 它是**脚本驱动**的（`/s script.scr`），而 AutoCAD 命令行里**空行等于重复上一条命令**，
   所以脚本不能有空白行（`AUTOCAD_DXF_SCRIPT` 与 `test_autocad_script_avoids_the_command_repeat_trap`
   守着这条）。它也不支持 `--version`，版本证据改从启动横幅读取（读取**按墙钟时间**设界，
   进程完全不输出也不会把探测卡死）。
3. **脚本会执行命令，所以文件名是数据、不是路径**：上传先落到固定内部名 `source.dwg`，
   转换输出固定为 `output.dxf`，脚本固定为 `converter.scr`，`argv` 里的输入永远是
   `<workdir>/input/source.dwg`。用户文件名（可能含换行、引号、控制字符）只进入 provenance/展示，
   绝不进入命令行或 `.scr` 内容。`test_converter_paths_never_contain_the_operators_filename` 用
   一个带换行的恶意文件名守着这条。

## 7. 两个真实的坑（已固化为测试）

1. **镜像块插入不能用标量缩放**：真实图纸里 `scale = (−2.856, +2.856)`，把半径乘标量会得到
   负半径。曲线必须携带 **2×2 线性变换矩阵**（局部定义 + 仿射），消费端才 `sample()`
   出世界坐标——镜像、嵌套旋转、非均匀缩放才能同时正确。`test_mirrored_insert_mirrors_its_block_geometry`
   守着这条。
2. **图层 id 会碰撞**：两个中文图层名（`管道`/`管道2`）sanitise 成同一个 ASCII id，第一版因此
   把两个图层合成一个。现在 `_assign_layer_ids` 按规范序分配并保证唯一（`layer_…`），
   `test_layers_whose_names_sanitise_identically_stay_separate` 守着。

另有一处**应用侧**的观察值得记下：编辑器对线宽使用 `vector-effect: non-scaling-stroke`
（线宽是屏幕像素，不随缩放变化）。因此导入的 `stroke_width` 默认取 **1.8**（编辑器视觉粗细），
而不是按图纸线重换算出的 8——否则放大后整张图会变成墨块。

## 8. 验证

| 检查 | 结果 |
|---|---|
| 后端单测 | `test_cad_dxf.py`（34）、`test_cad_dwg.py`（27）、`test_cad_import.py`（35）、`test_cad_convert.py`（21）、`test_cad_api.py`（16）、`test_cad_cli.py`（9），共 **142** |
| 交叉校验 | `test_reader_agrees_with_an_independent_dxf_library`：自研读取器与第三方 DXF 库（仅测试依赖）对同一份文件给出相同几何——避免“只有我们自己觉得对” |
| 离线质量 harness | 新增 `cad_import_contract` 用例（不调模型） |
| 前端单测 | `cadImport.test.ts` + `cadUpload.test.ts`（文件名判定 / 报告文案 / 上传请求形状） |
| 浏览器 e2e | `cad-import.spec.ts`：上传 DXF → 报告 → 自动打开导入文档 → 适应全部后全部图元在视口内 → 邻居图纸 revision 未变；`plan` 路由写入为零；非 CAD 文件在本地被拒（不发请求）；缺解码器时先给提示 |
| 真实图纸 | 同一份 DWG：AutoCAD 路线 **9757 图元 / 12 层 / 3.7 s**（其中 0 个块定义缺失）；早前 LibreDWG 对象流路线为 9242 图元 / 23 层，导出 SVG/PNG/DXF 回读 9242 实体，M2 派生层 0.44 s、0 findings |
| 测试隔离 | 装了 AutoCAD 的开发机不得改变测试观感：所有相关 fixture 都把 `EXECUTABLE_GLOBS` 置空（`test_autocad_console_is_normalized_away_when_not_installed` 等） |

## 9. 已知边界（不藏）

1. **符号语义仍需人工映射**：287 个块实例只把块名写进 `cad_block` 元数据；把它变成图例符号
   需要一张人工确认的映射表。
2. **填充**：agentcad 目前没有填充图元，实心填充以多边形表达（几何线条不丢，填充域语义丢失）；
   图案填充直接跳过并计数。
3. **弧/椭圆会被采样**为折线（默认 12/16 段，`curve_segments` 可调），圆保持原生圆；
   但**非均匀缩放块参照里的圆**会变成椭圆，此时也按变换后的曲线采样为闭合折线，并报
   `CAD_CIRCLE_APPROXIMATED`（保几何正确优先于保图元类型）。
4. **文字对齐码只映射了锚点**，垂直基线可能有半个字高的偏移；旋转文字只保留锚点。
5. **大图是单次写入**：9242 图元一次导入 ≈ 9 s、1 个 revision；不再分批，因为分批会牺牲原子性。
   这意味着一次导入的内存占用与请求时间随图元数线性增长，也意味着一次 undo 就能整体回退。
6. **不要对整张大图跑 M3 整理引擎**：448 图元 ≈ 45 s，9242 图元远超交互尺度；请用
   `region` / `element_ids` 限范围（这也是 M3 已知债务的一部分）。
