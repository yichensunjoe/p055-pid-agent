# REUSE_AND_PITFALL_LOG — P055-PID-Agent

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

## 2026-09-18 · e2e 端口要可覆盖，且必须在隔离数据库上跑（P055-PID-Agent）

- 场景：本机 8000 端口被另一个工作区的常驻后端占用（连着共享的 35 张真实图纸 dev DB），而 `playwright.config.ts` 的 webServer 固定 8000/4173 + `reuseExistingServer:false`，于是“想跑 e2e”就变成“要么清活库、要么跑不了”。
- 结论做法：把端口改为环境变量可覆盖（`PID_AGENT_E2E_API_PORT` / `PID_AGENT_E2E_PREVIEW_PORT`），`vite.config.ts` 的 preview proxy 读 `PID_AGENT_API_TARGET`，`fixtures.ts` 的直连 API 根地址由同一组变量推导；CI 不传变量即用默认值。数据库仍然由 config 注入到 `test-results/*.db`，所以永远不会碰活库。
- 关键经验：**验收脚本的破坏性要写进它的接口设计里**——只要“目标库/端口”是硬编码的，下一个人就会在错误的库上跑它；让端口和库路径可参数化，是把“别这么做”变成“做不到”的唯一办法。
