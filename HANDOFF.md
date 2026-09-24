# HANDOFF — P055-PID-Agent

> 交接文档：每次开新会话先读本文件。更新规则见 `AGENTS.md`「HANDOFF 交接规则」。

## 当前状态（2026-09-24 R27 —— **M7-2 Phase-3 已签 CLOSED：`origin/main = 28c4f96`（CI run `35837923170` 四 job success）。本地 `m7-semantic-first-synthesis = c35c393` 在已签内容之上多 carry 未签的 NL planner 纵切第一块（`device_phrases.py` + `m7_text_planner.py` + 17 条测试）与三笔 docs 提交；工作树基线已复验：ruff clean / `validate_contract()` `[]` / 后端 1519 passed**）

- **已签 vs 本地**：签进 main 的 `6a9bfe6`/`28c4f96` 是 `5509e16`/`405e8e3` 的 rebase 版，差异恰好是剥掉 NL planner
  （`device_phrases` / `m7_text_planner` / `test_m7_text_planner.py` 285 行 / `typesafe_planner.py` 的共享词汇抽取）
  与 docs 提交——即 **Gate 签的是纯 provenance 修复，NL planner 未进 main**。
- **远端 Gate 原话已核**（2026-09-24 读回 agentcad 项目「覆盖扩展方案裁决」会话，该会话即 Gate 签署线，映射已登记进
  `/Users/joe/ai/reasonix/chatgpt-threads.json`）：Gate 签 **CLOSED @ `28c4f96`**，授权 `git push origin 28c4f96…:main`
  **仅此 exact SHA**，并明确「不要把 `80d0ec0` 或 NL planner 本地 ancestry 一并带入」；签署后条件已满足
  （main 已推、main CI run `35837923170` 四 job 绿），**下一阶段（自然语言纵切）已获授权，可直接开工，无需再问 Gate**。
  纵切目标句：一句中文 → DiagramSpec → 已冻结 deterministic chain → UI 真实出图；第二句中文改语义 → 重画 → 导出。
  Gate 同时明确：不先扩 200+ catalogue、不做 CAD runtime ingestion。
- **下一步（NL 纵切第二块，已授权）**：把 `m7_text_planner` 接到 API + 面板（界面 agent 用 TypeSafe key 一句话出图），
  再做“第二句中文改语义 → 重画 → 导出”。开工前先把本地 `m7-semantic-first-synthesis` 缝合到 `origin/main`
  （rebase 掉与 `6a9bfe6`/`28c4f96` 重复的 `5509e16`/`405e8e3`，保留 planner 切片），每轮收口按惯例发 Gate 复核。
- 本地 `main` 分支仍停在 `4e661b1`（m6），落后于 `origin/main`；未见 `review/m7-2-phase3` 本地分支残留。

## 上一状态（2026-09-23 R26 —— Phase-3 候选在 `review/m7-2-phase3 = 28c4f96`（5 commit，CI 四 job 全绿），等 Gate 复核最后一轮 delta；`main` 仍是已签基线 `592e6b9`）

- **本轮做了什么（Gate 第五轮一个小 delta）**：把 provenance 的“identity ↔ version”从一个 version 集合改成**声明成数据的绑定表**
  `PROVENANCE_IDENTITY_VERSION_BINDINGS`（identity → 定义它的 version(s)），`MATERIALIZATION_PROVENANCE_VERSION_FIELDS`
  由它**派生**（不再手写，消掉会漂移的“两个平行 tuple”）；持久化命名空间从 9 个键 → **12 个键**
  （5 identity + 7 version，新增 `diagram_spec_schema_version` / `adapter_topology_digest_version` /
  `symbol_geometry_catalog_digest_version`）；validator 两个方向都查。
- **为什么**：上一轮证明的是较弱的不变量——“整体有个版本”；要证明的是“**每个 digest 都有定义它自己的那个版本**”，
  否则单个存下来的 digest 仍然无法解释。
- **mutation（参数化遍历绑定表，不是手写七条）**：逐个 identity、逐个它自己的 version 改名 → **7/7 全红**。
- **门禁**：在 cherry-pick 那棵树上实测（ruff clean / `validate_contract()` `[]` / 相关两文件 165 passed）；
  本地整仓 **1519 passed**；CI run `35837022297` 四 job success。
- **下一步**：等 Gate 复核 `6a9bfe6..28c4f96` 并签 M7-2 Phase-3 CLOSED；签后才 fast-forward main。
  之后立即做 NL 纵切的第二块：把 `m7_text_planner` 接到 API + 面板（在界面 agent 里用 TypeSafe key 一句话出图），
  再做“第二句中文改语义 → 重画 → 导出”。

- **本轮做了什么**：① 按 Gate 授权把 `592e6b9..1f6bbd6` 原样推到 **`review/m7-2-phase3`**（不动 main、不 squash）；
  ② Gate 远端 exact-SHA 复核后只留一个 blocker（provenance 可选），已修并在同一分支追加 **`6a9bfe6`**；
  ③ 开工 **NL 纵切第一块**：`m7_text_planner.py`（一句话 → DiagramSpec）。
- **P0-3（provenance 必须强制）**：`MaterializedLayout` 自己携带 `provenance_identities`（编译时算好）；
  `with_materialization_provenance()` 把**五个 identity + 各自版本**写进保留命名空间 `metadata["m7_materialization"]`，
  caller 的同名 key → **写前硬失败**（不覆盖、不合并）；revision 仍从 `result.document.revision` 读。
  一句概括：**attribution 是调用方的，provenance 是编译的。**
- **三条必须说的证据**：`audit=None` 仍留完整链（读持久化 `audit_record`，key 集合恰等于声明的九个）；
  caller 自带链 → 写前拒、revision 不变、elements 为空、history 只有 `create`；caller 的 attribution 仍生效且其 context 对象未被改动。
  mutation：去注入 → 3 红；改成覆盖 → 1 红；只记 digest 不记版本 → 1 红（第三条**第一次是绿的**，因为断言两侧同源，
  改成对**声明的 key 集合**断言后才真红）。
- **NL 纵切第一块**：`device_phrases.py`（两个 planner 共用的、与模型无关的短语/候选词汇）+ `m7_text_planner.py`
  （一句话 → 设备/连接候选（**代码**）→ TypeSafe 只在对查找无解处做选择 → 组装 DiagramSpec）。
  关键性质：**模型只能从候选里选**，所以幻觉设备/悬空管道无法进入引擎（没有 parse 步骤）；不确定的子句**报告并跳过**；
  未声明的位号**点名而不凭空造设备**。端到端用例：句子 → spec → step 5 finalize → materialize（图上文字就是句子写的位号）。
- **门禁**：ruff clean、`validate_contract()` `[]`、后端 **1516 passed**（本轮新增 17 条 NL + 四条 provenance）；
  同分支 CI run `35835758827` 四 job success。
- **下一步**：① 等 Gate 复核 `1f6bbd6..6a9bfe6` 并签 Phase-3（签后 fast-forward main，需授权）；
  ② 继续 NL 纵切：把 planner 接到 API + 面板（一句中文 → 出图），再做“第二句中文改语义 → 重画 → 导出”。

- **本轮做了什么**：执行 Gate 授权的 **Phase-3（Drawing Materialization & Production Wiring）**，
  最小 DoD 未扩大。新模块 `m7_layout_materialization.py`（合同 §15 白名单新增 `PHASE_3_MAY_IMPORT_THE_CONTRACT`），
  唯一输入 = finalized canonical layout（函数签名里没有 model/prompt/spec/document，**没有地方能塞坐标**）。
- **Gate 第一轮 HOLD 的两个 P0，已闭**：
  ① `labels` 参数**删掉**——标签文本从工程行的 `tag` 推导，并用**同一个纯函数**证明布局放的盒子就是该文本量出来的盒子
  （长度不同的改 tag 在这里拒；长度相同的改 tag 盒子相同、这一层看不见，由 Step 5 工程语义闸拦，**两闸各管一段**）；
  回读比对新增 `text`/`label`，`symbol.label` 写空并被回读**读**（不许假设空着）。
  ② **target baseline 在写之前冻结**：`require_empty_target()` + `MaterializationTargetNotEmptyError`；
  "空" = 没有任何工程内容（默认图层/`system_default` 属于空文档，放行），并发用**已有** `expected_revision` 原子拒绝、冲突为终态。
- **Gate 第三轮裁决**：`resulting_revision` 不许预测——拆成 `materialization_provenance()`（随写走的五个身份，不含 revision）
  ＋ `materialization_record()`（读 `result.document.revision` 闭合，**没有** revision 参数）。
- **门禁**：ruff clean、`validate_contract()` 0、后端 **1495 passed**（Phase-3 新增 43 条）；
  6 条实跑变异全部变红（标签通道 / 回读不比文案 / 不证盒子 / 去 preflight / 身份里塞预测 revision）。
- **下一步**：等 Gate 回答 push 方式（授权 push / 不 push 按 diff 复核 / 压成单 commit）；签后按 reply63 的方向裁决
  直接开始 **自然语言纵切**（一句中文 → DiagramSpec → 确定性链 → UI 出图 → 第二句中文改语义 → 重画 → 导出）。

- **Step 5 已签已推**：`2676378`（canonical layout identity）+ `592e6b9`（按 Gate 收口：**两道**保持证明 ——
  engineering semantic（tag/class/type/measurement/连接端点·端口·medium/方向/system·loop，**不含** symbol_key 与 layout_intent）
  ＋ layout-input（symbol binding / rendering kind / intent / adapter topology identity，结构相等、不新增身份轴）；
  并修正措辞：projection 是几何投影，canonical layout digest 比它宽（tag 变、坐标不变 → projection 相同、digest 必须不同）。
- **Gate 裁定**：Step 1–5 与整个 **M7-2 Phase-2B Release Gate = CLOSED @ 592e6b9**（CI 四 job 实测全绿：
  Backend 3.11 / Frontend Node 24 / Browser Chromium / M5 72-case）。
  **Phase-3 已授权，最小 DoD 已固定**（不再扩大）：唯一输入 = finalized canonical layout；确定性 materialization；
  完整落图且不改工程语义；**只复用现有 production writer**（`POST /api/v2/documents/{id}/transactions` →
  `DocumentService.apply_transaction`）；provenance 全链（DiagramSpec → topology → snapshot → canonical digest →
  materialization digest/version → production transaction → revision）；先用 A/B fixture 证明单份无坐标 DiagramSpec 真能出图。
- **方向审视（新开会话，独立线）**：远端结论是「方向没根本性偏差，但顺序偏了」——应**暂停纵向加深布局证明**，
  把 Phase-2B 降级为「稳定编译后端」，立即打通 **NL → DiagramSpec → 确定性链 → UI → 自然语言修改 → 导出** 的
  产品纵切（每轮验证上限三层：contract / regression / product E2E；CAD 不再作主验收对象）。
  这与 Gate 的 Phase-3 授权不矛盾：Phase-3 正是产品纵切所需的最后一公里，做完立即转 NL 纵切。

## 近期轮次（2026-09-23 R22 —— Step 4 已签已推；Step 5 本地完成等签）

- **`origin/main = f1acb21`**（Step 4：内容测量 + 派生画布 + 最终几何校验 + stroke 包络 + 包含证明）。
  Step 5 本地：**`2676378`** → **`592e6b9`**（两道保持证明的分层修正）。
- **Step 5 交付**：新模块 `auto_layout_identity.py`（闸门一：topology 侧工程 digest vs **从 plan live 字段重建**的同一 digest，
  两侧共用一种行形状但**不共用一次调用**；闸门一的另一半是纯几何覆盖：设备/连接各恰好一次、不许凭空多）；
  canonical projection（三表合一、7 字段、量化、`-0.0` 归一、重复 sort key 与未声明行字段硬失败）；
  digest 信封 = `LAYOUT_DIGEST_INPUTS` 闭集（9 个 volatile 名逐个硬拒绝）；replay 只比身份。
  plan 新增 `engineering_systems` / `engineering_entities` / `PlanConnection.tag`（引擎收到的工程事实记录），
  `SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION` **/6 → /7**；`LAYOUT_DIGEST_VERSION`、`LAYOUT_PROJECTION_VERSION` 不动。
- **Step 5 门禁**：ruff clean、`validate_contract()` 0 problems、后端 **1446 passed**（新增 58 条）、
  6 条实跑变异全部变红（忽略闸门 / 丢覆盖 / 取消排序 / 放行 volatile / 去量化 / 清空 snapshot digest → 13 红）。
- **签后一条命令**：`git push origin 2676378:main` → 等四 job → 按网关裁定进入收口（Phase-2B release gate 或 Step 6）。
- **TypeSafe 画图**：UI 侧 key/Base URL/model/开关（`App.tsx` typesafe-settings）+ 后端
  `typesafe.py` / `typesafe_planner.py` / `/provider/typesafe/status|verify` / `/documents/{id}/agent/typesafe-plan` 均已落地。

## 近期轮次（2026-09-23 R21 —— Step 3 已签已推；Step 4 经两轮 Gate 修正后等签）

- **`origin/main = c439e47`**（Step 3：冻结 symbol geometry + 端点绑定 + 正交路由 + 标注）。
  Step 4 range（无共享文档改动）：`ad00e2c` → `1b0d4dc` → `6daa563` → `f1acb21`。
- **Step 4 交付**：`auto_layout_canvas.py`（content bounds 闭集 = 节点/管路/标注的**渲染范围**；margin 按 density 档位；
  aspect class 是比例且只增不减；orientation 定方向；无裁剪校验；端点 escape 段收窄的交叉检查）、
  `PRESENTATION_STROKE_POLICY`（symbol_outline 1.5 / connector 1.75 / leader_line 1.0 / annotation_text 0 的 reach，
  归 `layout_rules_version`，不新增 digest 轴）、`m7_symbol_geometry` 冻结时校验
  `unstroked shape bounds ⊆ intrinsic box`（含 SVG 端点→圆心圆弧参数化）、契约 §13.1–§13.9、任务书 §13、
  plan 字段集 +`content_bounds` → plan digest `/6`。门禁：ruff clean、两份 `validate_contract` 空、
  **pytest 1386 passed**（本轮起点 1373）、14 条变异全部确认变红（其中 3 条“第一版全绿”已补 fixture）。
- **历史改写（按网关裁定）**：记账提交 `b5e1765`（HANDOFF/踩坑日志）已 **drop**，range 内不再含共享文档改动；
  两个文档仍在**工作区**更新（未提交）；记账与本轮可回溯信息写在 `.freebuff/remote-bridge/m7-step4-accounting.md`
  （`.freebuff/` 被 .gitignore 忽略，所以它是记录位置而非提交位置）。旧 tip `b307dae` 仍可从 reflog 找回。
- **签后一条命令**：`git push origin 6daa563:main` → 等四 job → 直接进 Step 5（canonical projection →
  语义保持校验 → `canonical_layout_digest` → deterministic replay），网关已批准不必再申请开工许可。

## 近期轮次（2026-09-23 R19 —— **等远端签 Step 2 checkpoint（`ef0fb1d..c319e4e`），本地已就绪，卡在浏览器扩展断线**）

- **`origin/main = 9ab3900`**（M7-2 Phase-2B Step 1，CI 35807302063 四 job success；Phase-2A 在其下 `1f2de99`）。
  本地 `main` 仍是 `4e661b1`（M6 Phase-2B-1，未签）——**M7 线一律显式推 SHA**（`git push origin <sha>:main`），从不推本地 main。
- **待签的两个提交（未 push）**：`ef0fb1d`（Step 2 确定性放置）＋ `c319e4e`（Step 2 唯一 blocker 的修复：
  `grouped_by_zone` 无 zone 语义时**硬拒绝**，删掉 `GROUPING_FALLBACKS`，plan digest 升 `/3`）。
  门槛已过：ruff clean、两份 validate_contract 空、pytest 1258 passed。
- **卡点（非代码）**：`.freebuff/remote-bridge/53-chat-body.txt` 已写好（Step 2 修复报告），但 `bsk` 报
  `0 browsers connected` —— 浏览器扩展断线，重发前需要人把浏览器/扩展连回来。连回后一条命令：
  `bash .freebuff/remote-bridge/send.sh .freebuff/remote-bridge/53-chat-body.txt` → `bash .freebuff/remote-bridge/poll53.sh`。
- **Step 3 方向已收（reply52）但按纪律未开工**：`SymbolRegistry` → 冻结 `SymbolGeometryCatalogSnapshot`（只给
  事实/约束，不给 x/y/rank/canvas/route）→ 先 materialize 几何并**必要时重算 placement** → 才 routing；
  新增 `symbol_geometry_catalog_digest`（`m7-symbol-geometry-catalog-digest/1`）进 `LAYOUT_DIGEST_INPUTS`，
  `LAYOUT_DIGEST_VERSION` 升 `m7-layout-digest/2`，`layout_rules_version` 不被偷塞。
- **本轮关键教训**：① "假的守卫"不只指假绿测试，也包括**变异选得不对**——我第一次的"引擎假装排版"变异
  只加了注释，测试当然不红，重做成真的写入 `(0,0)` 才有 5 条变红；② 校验器里 `.index()` 在名字缺失时会
  抛异常而不是报问题，等于让坏声明"无报告"（已改为先判在不在，再比位置）；③ 一条测试断言
  `preview_document` 有 `preserve_positions` 参数，实际它是 `AutoLayoutRequest` 的**字段**——
  断言写错方向会让守卫看着存在但什么都没验。
- **M7 已完成**：Phase-1 `b339039` → **Phase-2A CLOSED @ `6250f93`**（CI 35802706611 + Visual baselines
  35803207406 双绿）。交付：两轴 assessment（valid × complete）、逐操作回执、`synthesis_proposal_evidence`
  append-only（schema v9/v10）、后台拒绝 partial 完成会话、六分支前端决定循环、确认界面显示 197/120/77。
- **M7-2 Phase-1 Design & Contract = CLOSED @ `07e93d6`**：`m7_layout_contract.py` + `docs/m7-2-deterministic-layout.md`
  + 绑定/表层/变异测试；含 digest/projection 版本号、数值规范化（1e-6、禁 NaN、-0→0）、复合排序键
  `(placement_kind, engineering_id)`、bounds 归包络、`preserve_positions` 按路径分流。
- **已授权开工：M7-2 Phase-2A — DiagramSpec Adapter**（远端 reply49）。只做 adapter：
  `DiagramSpec → validate → adapter → 确定性语义拓扑输入 → STOP`；禁止产生 x/y、width/height、waypoints、
  走线、标注、画布，禁止改 AutoLayoutEngine 放置算法，禁止新 HTTP/MCP/UI 表层。fixture A 正向、C 硬拒绝（不得静默 strip）。
- **本轮关键教训**（已写入项目内日志）：① 声明在一边、事实在另一边——`global_failure_reason` 只在证据行、
  assessment 上没有，导致合法的 `not_evaluated` 被新前端判成契约违规（写边界测试时才发现）；
  ② 未被 push 但**真实库已执行过**的迁移等同于已发布：v8/v9 都不能原地改，只能加 v10；
  ③ 违规 token 若匹配既有表层名（`auto_layout` vs `preview_auto_layout`）会把 legacy 报成新违规；
  ④ 单字段排序键证明不了全序，而裸字符串排序键会静默变成字符列表。

## 近期轮次（2026-09-23 R17 — M7-2 Phase-2A CLOSED / Phase-2B Step 1）

- **`origin/main`：`07e93d6`（Phase-1）→ `1f2de99`（Phase-2A，已签已推）→ 本地 `9ab3900`（2B Step 1，待签）**。
- **Phase-2A 交付**：`m7_diagram_spec.py`（wire-form 几何硬拒绝：按字段路径拒绝、相对锚点同罪、
  **拒绝而非 strip**）+ `m7_diagram_adapter.py`（`SemanticTopology`、canonical `(kind, engineering_id)` 排序、
  `spec_semantic_digest == topology_semantic_digest` 无损等式、独立版本化的 adapter digest）。
  gate：1152 passed；三条变异（去扫描 / 让锚点过 / 去排序）均变红。
- **Phase-2B Step 1 交付**：`auto_layout_semantic.py` + 契约 §8.2 + 任务书 §11 + 两类测试（1203 passed）。
  `LAYOUT_INTENT_CONSUMPTION` 把六个维度的"接收步 / 施加步"变成可对拍的数据；
  `CANVAS_DERIVATION_CHAIN` 十段焊死"画布是输出"。
- **向远端提的两个新问题**：Step 2 的 `density` 是映射到既有 `node_gap/component_gap` 离散档位，
  还是由 Step 2 自己定义间距规则并把规则版本作为 digest 输入（我倾向前者/后者中的后者）；
  以及 Phase-2A 文本扫描收窄是否挂到具体 milestone。

## 近期轮次（2026-09-22 R16 — M6 Phase-2A CLOSED / Phase-2B-1 边界）

- **`origin/main = 3147a22`**（Phase-2A final anchor；本地 `main` 领先 1：`4e661b1` 未 push）。
- **M6 Phase-2A 收尾**：`156481a`（治理核心）。随后远端发现报告一处长度不一致 → 查清是**文档自相矛盾**
  （同节一处 `m6patch_<digest[:16]>`、一处 `m6patch_<sha256>`，而代码一直是对的），修法不是改文字而是
  **把五类持久身份格式降为契约数据**（`PERSISTENT_IDENTITIES` / `identity_prefix()`），并让测试从任务书里
  正则抽出所有 ``前缀<宽度>`` 与声明对拍 → `3147a22`。该提交的第二个守卫**第一版是假绿**（断言带引号的
  `"m6prefix_"`，而真实字面量是 f-string），靠变异检查才发现，已记入 `REUSE_AND_PITFALL_LOG.md`。
- **M6 Phase-2B-1 已开工**：`4e661b1` 落契约第 §12 节（写入顺序 / 授权绑定 / attempt↔transaction 分离 /
  补偿模型 / 原子性声明）+ `m6_governed_models.py` + 20 条契约测试。**关键发现**：`store.save` 本身
  就是一个 `BEGIN IMMEDIATE`，工程 revision 与它的 provenance 已同事务，所以
  `TWO_PHASE_RECOVERY_PROTOCOL_REQUIRED = False`（缝存在与否由契约规则强制二选一，不允许沉默）。
  剩下的 schema v8 / 服务 / 七条拒绝路径尚未写。
- **更早一轮**：`origin/main = 0db2585`。
- **M5 收口**：`reports/m5-release-readiness/`（md + json：六条 track 的 anchor / commits / CI+Visual run / 不变式 /
  证据指针 / **重开触发条件** / `claims_not_made`）+ `scripts/m5_release_readiness.py`（身份**重算**而不是照抄）+
  `backend/tests/test_m5_release_readiness.py`（同一 verifier 跑在 CI 里；三条变异都会红）。
  CI **35710996168** 四 job 全 success。状态：deterministic v3 CLOSED・A5 CLOSED・B CLOSED・TypeSafe ACCEPTED・
  A6 readiness CLOSED・**A6 real-model qualification BLOCKED_EXTERNAL**（外部 provider 依赖，不阻断发布）。
- **M6 已开**（远端 `reply26` 固定十条口径）：域名 **Governed Semantic Ingestion**，第一阶段**只做设计与契约**。
  交付：`docs/m6-governed-semantic-ingestion.md`（任务书）+ `backend/agentcad/m6_ingestion_contract.py`（治理契约数据）
  + `backend/tests/test_m6_ingestion_contract.py`（文档 ↔ 数据一致性 + 表层边界 + 变异）。
  **已推送**：`4b2c1be`（初稿）+ `a35d20f`（六项裁定并入），`origin/main = a35d20f`，
  CI **35729718047** 四 job 全 success（940 passed / ruff clean / `gate_failures []` / digest `96b999fa…` 未漂）。
  **M6 Phase-1 Design & Contract = CLOSED**；下一阶段 = **Phase-2 Minimal Governed Vertical Slice**（仅 domain/service 层，暂不开表层、暂不接 R8 批量数据）。
- **未提交**：M6 三个新文件 + `PROJECT_CHARTER.md` §51 改写；`HANDOFF.md` / `REUSE_AND_PITFALL_LOG.md`
  （里面同时有另一会话 R8 的未提交文本，所以不整文件提交）。未跟踪：`.workbuddy-ai/`、`reports/pid-repro/`（另一会话产物）。
- **下一步**：把 M6 任务书通报远端 Gate（十条口径的落地形式 + 六个未决问题的裁定请求）；签署后再进入
  candidate schema 实体化与 review queue 表层（**那时才登记表层**）。

## 本轮进展（2026-09-22 R13：**M6 Phase-2A 已做（Governed Candidate Core），等 integrity fix 后的 Gate**）

- **远端裁定（`reply28`/`reply30`）**：Phase-1 Design Gate **已签**（`a35d20f` 已 push，CI 35729718047 全 success）；
  Phase-2A **授权开工**，边界：仅内部 domain/service 层，**暂不开 HTTP/MCP/UI**、不接 R8 数据、不接 TypeSafe/LLM 摄取、
  不做 apply-v2/undo。
- **已实现**：`m6_candidate_models.py`（domain schemas）+ `m6_candidate_core.py`（治理核心）+ schema **v7**
  三张 insert-only 表（`semantic_candidates` / `review_decisions` / `confirmed_semantic_findings`）+ store 方法。
  三个关键设计：**状态从 append-only 日志回放而不是读列**；**`patch_id = m6patch_<完整 digest>`**；
  **编译器拒绘不猜**（overwrite → conflict，destructive → forbidden，未支持意图→ reason code）。
- **integrity fix（第一遵）**：`review_decisions`/`confirmed_semantic_findings` 内部链改为**真外键 +
  composite FK + ON DELETE RESTRICT**（仍不引用 `documents`）；确认的 decision + finding **同事务**提交
  （故障注入测试证明）。schema v7 未发布，按远端裁定**原地修**，不造 v8。
- **identity fix（第二遵，两个 blocker）**：① 编译生成元素 id 从 64 bit 改为**完整 256 bit**
  （`el_m6_<sha256>`，它将成为真工程对象身份）；② `review_decision_id` 改为
  **`review-decision-v1` + 决策的不可变内容**（candidate/kind/from/to + reviewer_identity/action/note +
  baseline revision·identity·path·digest_version·digest + resolution/successor），`decided_at` 排除——
  否则“同一位工程师基于 revision 7 与 revision 8 的两次不同确认”会碰成同一个审计事件，
  而 `(candidate_id, review_decision_id)` 是 finding 的外键基础；③ 语义值规范化改为
  **path-aware**（`equipment_tag`/`equipment_class` → identifier 规则；`annotation_role`/`existence` → exact；
  未登记 path → hard fail，不回退 tag 规范化）。
- **门禁（本地）**：`ruff` ✓・`pytest -q` **988 passed**・harness ✓・acceptance `gate_failures []`・
  `validate_contract()` []・无新增 HTTP/MCP/UI 表层・`reports/m5*` 一字未改。
- **证据**：`scripts/m6_phase2a_walkthrough.py` → `reports/m6-phase2a/walkthrough.txt`（正向链 / replay digest /
  apply 拒绝 / baseline 漂移 / cascade 存活），并由测试**执行**（不是引用）。
- **下一步**：签 Phase-2A Gate 后进 **Phase-2B**（apply-v2 governed write + provenance + compensating undo + replay）。

## 本轮收口（2026-09-22 R12c：**M6 Phase-1 Design Gate 已签、已 push、CI 全绗**）

- **远端裁定（`reply28`）**：M6 Phase-1 Design Gate **签署通过**；十四条逐项 PASS（含 region 多 selector +
  revision identity、review 独立生命周期、authoritative baseline + optimistic conflict recheck、calibration 独立
  Gate、canonical patch + semantic poststate replay、raw byte equality 禁止、undo 为补偿事务、Phase-1 无偷跑）；
  授权 `git push origin a35d20f:main`。
- **已推并验证**：`0db2585..a35d20f` 两条提交；CI **35729718047** 四 job（Browser / M5 72-case gate /
  Backend 3.11 / Frontend）全 success；3.11 上 `pytest` **940 passed**、ruff `All checks passed`、
  acceptance `gate_failures []`、`core_corpus_digest 96b999fa…` 与冻结 v3 一致。本阶段无 UI 变化，未 dispatch Visual。
- **记账口径**（远端）：`REUSE_AND_PITFALL_LOG.md` 内容仍是未提交本地文本，所以那两条 pitfall 不算 `a35d20f`
  的发布交付物；不阻塞，继续维持共享文件隔离规则。
- **下一步（远端已定边界）**：**M6 Phase-2 — Minimal Governed Vertical Slice**：
  `source artifact → SemanticCandidate → review decision → ConfirmedSemanticFinding → deterministic patch
  compilation → apply-v2 dry-run / conflict check → 单次 governed write → provenance + undo/replay evidence`。
  先做内部 domain/service 层最小闭环，**暂不开放 HTTP/MCP 表层，也暂不接 R8 的 112×2 批量摄取**。

## 本轮修订（2026-09-22 R12b：**远端六项裁定并入契约 —— 含一处被明确要求改掉的既有选择**）

- **远端对 `4b2c1be` 的定性（`reply27`）**：架构全 APPROVED（candidate/patch 分离、producer 权限模型、
  review 状态机、apply-v2 唯一写者、破坏性默认拒、provenance 模型），但 **push 暂未授权** —— 六项裁定必须
  写回任务书与机器契约，replay 契约需要语义修改。要求**在其上再加一个纯 design/contract 提交，不 amend**。
  仍不得加 runtime / HTTP-MCP 表层 / 持久化实现 / 真值语料。
- **裁定①区域**：三类 selector 都存（`geometry_selector` / `element_refs` / `text_spans`）+ frame 字段；
  `region_id` 的契约是「同一 source revision 下可确定性重定位」而非「坐标不变」，
  **revision 变则必须产生新区域身份**。
- **裁定②review 持久化**：**同库、独立聚合**；三实体各有不可变 ID；**删除文档/元素不得级联删审阅历史**；
  「确认并应用」可同事务，但 `review_decision_id` 只能被引用、不能被折叠。
- **裁定③conflict 基准**：以 **apply-v2 管理的 current committed engineering semantic state** 为唯一权威基准
  （不是原始图纸 / candidate / TypeSafe 输出）；比较键 = 目标工程身份 + 语义路径；**apply 前必须重读 baseline**，
  `reviewed baseline != current authoritative value` 即 `conflicted` —— 即**乐观并发控制**，并配套新机器不变量：
  `confirmed + baseline changed before apply → conflicted → 禁止进入 applied`，解除 conflict 必须产生
  **新的** reviewer decision。
- **裁定④校准**：`measured_on_gold_corpus` 在 v1 **默认禁止**，必须独立 Calibration Gate（仍由远端 Release Gate
  签）并附八项证据；**不得写「达到 N 条即自动 calibrated」的规则**（`CALIBRATION_CLASS_AUTO_PROMOTION_RULE = None`）。
- **裁定⑤replay（唯一被要求改掉的既有选择）**：删掉「原始序列化字节完全相同」，改为两层 digest ——
  `REPLAY_CANONICAL_PATCH_MUST_MATCH` + `REPLAY_SEMANTIC_POSTSTATE_MUST_MATCH`，显式排除
  `transaction_id` / `timestamps` / `audit_time` 等 volatile provenance；冻结输入加 `authoritative_baseline_revision`。
  远端指出这与 A5「把运行时 metadata 混进身份」是同一个错 —— 已记入 `REUSE_AND_PITFALL_LOG`。
- **裁定⑥undo**：不增加 candidate 级 `reverted` 状态（本地原选择被批准）；但 **transaction 层**必须能表达
  `TRANSACTION_STATES = applied / reverted / superseded`，界面需要时用派生展示态
  `confirmed + last applied transaction reverted`，不得变成新的权威 candidate 状态。
- **自检新增 5 类检查**：区域 selector 与 revision 换身份、review 持久化三条、conflict 基准与乐观并发
  （含「入 `applied` 必带 `baseline_recheck`」与「存在 `confirmed → conflicted` 回落边」）、校准闸、
  replay 两层 + 排除 volatile。测试从 23 条增到 **33 条**，新增 5 条变异含**baseline-change → conflicted** 那条。
- **门禁（本地）**：`ruff check backend` ✓・`pytest -q` **940 passed**・`validate_contract()` 无违规。

## 本轮进展（2026-09-22 R12：**M6 第一阶段（设计与契约）本地完成 —— 任务书 + 可被反驳的治理契约**）

- **远端十条口径的落地形式**（`reply26`）：① `SemanticCandidate` **独立建模**，现成的 `SemanticDiff` /
  `StructuredEngineeringPatch` 只在 `confirmed → compile → apply-v2` 段复用；② 规则引擎 / TypeSafe / repair-LLM
  职责分离，**都只产出 candidate**；③ **confidence 永远不等于 authority**；④ review queue 是显式状态机，
  **禁止 `proposed → applied`**，人工确认必须记录 reviewer action；⑤ 人工确认的是**事实**不是裸 patch；
  ⑥ **apply-v2 是唯一生产写入口**；⑦ 删除 / 覆盖 / 拓扑替换默认禁止（"模型认为旧内容错了"首先产生
  conflict candidate）；⑧ provenance 要能回答四问；⑨ **undo 与 replay 分开**；⑩ gold corpus 从零建
  （R8 的 112×2 只能进 `research_examples/` / `candidate_seed_material/`）。
- **架构的机器形式**：八层链 `imported_artifact → source_region → semantic_candidate → review_decision →
  confirmed_semantic_finding → structured_engineering_patch → apply_v2_transaction → committed_revision`，
  每层一个不可变 id；`validate_contract()` 断言**有工程写权限的层恰好是 `apply_v2_transaction` 一个**。
- **两条性质是"遍历声明的边"验证出来的，不是表格里的一句话**：`applied` 只有一条入边且来自 `confirmed`；
  whitelist 为空时进入 `confirmed` 必须带 `reviewer_action` + `review_decision`。v1 的
  `AUTO_ACCEPT_WHITELIST = ()`——要开任何自动接收类别必须单独签一个 Gate。
- **自检抓到我自己一个真错误**：我一开始给 `committed_revision` 也标了工程写权限；它记录的是事务产出的**结果**，
  权限属于事务而不属于被它创建的 revision。这正是"把权限写成数据"的用处。
- **未决问题（明确留给 Gate，不自作主张）**：`source_region` 的形式、review queue 的持久化位置、conflict 的
  比较基准、`calibration_class` 何时可写 `measured_on_gold_corpus`、replay 的比较粒度、是否需要独立的
  `reverted` 状态（本任务书选择不加，撤销记录在事务层）。
- **门禁（本地）**：`ruff check` ✓・`pytest tests/test_m6_ingestion_contract.py` **23 passed**・
  `validate_contract()` 无违规・文档 ↔ 契约逐名核对（层 / 状态 / 禁止边 / 策略意图 / 语料维度 / 候选字段 /
  producer）✓・活的 OpenAPI + MCP 表层里没有任何 candidate/ingestion 形状的路由 ✓。

## 上一状态（2026-09-22 R10：**B（重试契约完整性）与 TypeSafe（判读式画图 + 凭据卫生）都已进 `origin/main`**）

- **`origin/main = 72f364e`**（本地 `main` 同步于此，无领先提交）。本地分支 `local-pre-replay = 925ced2` 保留了重放前的旧 TypeSafe 链（仅供对照，已不在 origin）。
- **五个提交，两段**：

  ```
  5fc0964
    └─ 263808f  test(m5): check the other half of the attempt contract…   ← B（CI 35706204970 success / Visual 35706717144 success）
        └─ eabb58c  feat(agent): configure a TypeSafe key in the panel…
            └─ 50653a5  feat(agent): report which TypeSafe key…
                └─ 7ce1395  test(agent): refuse to let a TypeSafe credential reach the repository
                    └─ 72f364e  fix(agent): give the TypeSafe fields names of their own…  ← 修 7ce1395 的 CI 红
  ```

  CI **35707486170 (72f364e)** 四 job 全 success（3.11 上 `pytest` 905 passed），Visual **35707535636 (72f364e)** success。
- **一次真实的 CI 红（必须记住）**：`7ce1395` 的 Browser job 因 `strict mode violation: getByRole('textbox',{name:'Base URL'}) resolved to 2 elements` 而红——
  我把新字段命名为「TypeSafe Base URL」，而 Playwright 的 `name` 默认**子串匹配**，旧字段的选择器不再唯一。
  本地 node 单测发现不了（不渲染 React）。修法：改名 + `frontend/e2e/typesafe-panel.spec.ts` 4 条 hermetic 断言
  （字段名唯一 / 来源提示 / 空 key 报错 / typesafe-plan 路由且 body 无 `api_key`），并用标签改回去的变异验证它确实会红。
- **远端裁定要点（reply23）**：① B 正式改写为 **F6 retry-contract integrity + first-attempt regression guard**（不再追整体 S@1）；
  ② 旧的 `39d8b75` **不得**从 TypeSafe 链上 push（ancestry 里含两个 TypeSafe 提交），必须从 `5fc0964` 干净重放；
  ③ 重放后 patch-id 一致、门全绿则**预授权**直接 push；④ TypeSafe 在 B 之后重放到新 main 上，并加一条凭据硬检查。
  四件事都按此执行：`39d8b75→263808f`、`2270ab6→eabb58c`、`a514bc3→50653a5` 的 patch-id 两两**完全相同**。
- **B 的内容**：acceptance 回归测试同时钉——无重试契约的 case 首轮必须成功；只有 F6 能声明重试且
  `required_attempts` 必须等于冻结 `F6_CONTROL_FLOW[operator_id]` 且恰在该轮收敛；`S@1 == 无契约 case 数 / 总数`
  （动态推导，不写死 60/72）。反向断言能变红由 synthetic case 单独证明。
- **TypeSafe 第三个提交（凭据卫生）**：`backend/tests/test_typesafe_credential_hygiene.py` 扫全部文本文件找**形状**
  （`apikey_` 字面量 / 24 位以上 Bearer / `TYPESAFE_API_KEY=<字面值>` / JSON body 里 24 位以上 api_key），
  并断言 CI workflow 一个字都不提 TypeSafe 凭据、提交的 evidence 只记录来源与有无；扫描器自身对合成样本会报红。
- **门禁（本地，final SHA `72f364e`）**：`ruff` ✓・`pytest -q` **905 passed**・harness 9/9・acceptance **72/72**
  （六 family S@5 全 1.0、`gate_failures []`、safety 13/13、`core_corpus_digest 96b999fa…`）・coverage exit 0・scale exit 0・
  qualification exit 3（按契约）・前端 `npm test` **149 passed** + build ✓・`npm run test:e2e` **56 passed / 1 skipped**
  （视觉基线 10/10 未变）・shared 2 passed・secrets ✓・面板 Playwright 检查 PASS・live acceptance PASS（真 key）。
  本机 e2e 用 `PID_AGENT_E2E_API_PORT=8137 PID_AGENT_E2E_PREVIEW_PORT=4391`（默认 8000 被另一会话占用，8123 被一个 http.server 占着）。
- **未提交**：`HANDOFF.md` / `REUSE_AND_PITFALL_LOG.md`（里面同时有另一会话 R8 的未提交文本，所以不整文件提交）。
  未跟踪：`.workbuddy-ai/`、`reports/pid-repro/`（另一会话产物）。
- **下一方向**：远端排期 **A6 qualification**——已做完**零代码 readiness check**，见下节。

## 本轮进展（2026-09-22 R11：**A6 readiness check：其余前置全满足，唯一缺口是 provider 凭据**）

- **远端裁定（reply23）**：B = **CLOSED**（语义固定为 F6 retry-contract integrity + first-attempt guard，不是追 S@1）；
  TypeSafe = **ACCEPTED @ 72f364e**；**不做** F6 预期重试 UI（那是 benchmark 诊断，已在 spec/report 里有契约）；
  下一步只做 A6 **零代码 readiness check**：先查清合约与本机凭据，有凭据就跑 24 条，没有再报 readiness 并停。
- **合约（读代码得出）**：OpenAI-compatible 端点；`PID_AGENT_LLM_BASE_URL` + `PID_AGENT_LLM_MODEL` **两者必须有**
  （`PID_AGENT_LLM_API_KEY` 可选，别名 `AGENTCAD_LLM_*`）；用例集 = dev 4/family × 6 = **24**（已实跑确认）；
  门槛 `model_s5_overall ≥ 0.80` + `model_family_min ≥ 0.5` + safety smoke 100%；退出码 0/2/3。
- **本机扫描（只看名字）**：仅 `~/.zshrc` / `~/.zshenv` 里的 **TypeSafe** 凭据；无 `PID_AGENT_LLM_*`；
  无 Ollama(11434)/LM Studio(1234)（5000 是 macOS AirPlay）；`.env` 只有 `.env.example`；`launchctl getenv` 无。
  → 项目自己的 qualification 凭据在本机**不存在**，且**没有**拿 TypeSafe key 顶替。
- **其余前置已满足**：设两变量指向 loopback 探针（不发请求）后 `configured_provider()` + `ModelRepairPlanner` 成功构建，
  identity 带 `provider_class='openai-compatible'`、`prompt_fingerprint db205459…`、`schema_fingerprint f2b11350…`；
  oracle/orchestrator/safety/证据复算器由 CI 905 tests 覆盖；deterministic 侧未动（acceptance 72/72、digest `96b999fa…`）。
- **产物**：`reports/m5-qualification/readiness.md` + `readiness.json`（`status=awaiting_real_model_qualification`，exit 3），
  commit **`a92044f`**（本地，**未 push**）。凭据到位后的命令写在报告第 4 节。
- **远端裁定（reply24）**：**A6 readiness = CLOSED**；**A6 real-model qualification = BLOCKED（外部 provider 依赖）**；
  M5 deterministic v3 / A5 / B / TypeSafe 各自已收口。明确要求：**不要为了消掉 exit 3 随便接一个端点**，
  也不要拿 TypeSafe 顶替 repair planner provider；只有拿到“真正准备采用的 repair-agent 模型配置”才重新打开 A6。
  `a92044f` 已获授权 push（已推，`origin/main = a92044f`，CI **35708328119** 四 job 全 success；
  readiness 证据不要求单独跑 Visual baselines）。
- **重新打开 A6 的条件（写给下一个会话）**：有 `PID_AGENT_LLM_BASE_URL` + `PID_AGENT_LLM_MODEL`（+ 必要时 `PID_AGENT_LLM_API_KEY`），
  且该 provider/model 就是实际候选；届时直接跑冻结的 24-case dev qualification，**不改** threshold/corpus/prompt contract/oracle/family 分母。
  结果语义固定：exit 0 = QUALIFIED、2 = 真跑了但未达标（真实能力发现，不得调低门槛解释）、3 = 前置缺失。
  证据写 `reports/m5-qualification/qualification.json` + 同目录 artifact，同时记录**非秘密 identity**
  （provider_class / base_url_class / model / planner_version / prompt_fingerprint / schema_fingerprint / candidate_sha / core_corpus_digest）；
  不得提交 key、Authorization header、带密请求体或可反推凭据的日志。
- **可继续的下一步**：远端说“不必等 A6”，可进入下一里程碑（尚未定义）。

## 上一状态（2026-09-22 R8：**真实图纸 `气路系统总图.dwg` 在本地 pidagent 上复现完成 + TypeSafe 语义索引**）

- **本轮性质**：用户驱动的**演示/验收轮**，非 Charter milestone。不改架构、不改版本轴、不动语料；
  git 工作树干净（`f60693d` 之上无新提交），产物全部落在 `reports/pid-repro/`。
- **拉起**：`.venv/bin/agentcad serve --host 127.0.0.1 --port 8000`（后端同时托管已构建前端）。
  旧进程（Sep 21 10:02 启动，早于 `f60693d`）已优雅 TERM 重启，新 PID 13802，`/health` 200。
  前端 dist（Sep 21 15:44）经比对**不比任何前端源码旧**，未重建。
- **计时（本次实测，口径见报告）**：
  | 阶段 | 服务端 | 端到端 | 产物 |
  |---|---|---|---|
  | 只读预演 `plan`（冷启动） | 4,197 ms | 4.21 s | — |
  | 全幅复现 | 4,001 ms | **8.37 s** | `doc_d17f1c7d174f` · 9757 元素 |
  | 图面区域复现 | 3,895 ms | 7.93 s | `doc_72dabc6268d8` · 9494 元素 |
  | TypeSafe 语义索引 | — | 3.11 s | `semantics_candidates.json` |
  **「画完」= 8.4 s**（含 939 KB 上传 + 9757 图元响应回传；服务端自身 4.0 s）。
- **复现保真**：`autocad-core-console` 路线 9757 图元 / **0 块定义缺失**，对比仓库既有 LibreDWG 文档
  `doc_32774539f798` 的 9242 / 157 缺失（多 515 图元）。图层 12 个全保留（含中文图层名
  `阀门`/`管道`/`覆盖气`/`仪表`/`取样`）。诚实缺失报告：hatch 边界 128、旋转文字 104、图案填充 7。
- **图幅发现**：图纸自身 extents 被游离内容撑到画布 **58273.8×4772.2**，图面本体只占最右 5437×3347
  （9494 元素）；左侧 256 条散碎多段线（`0` 图层）+ 中部 7 条设计待办文字是撑开画布的元凶。
  故保留**两份**：全幅版（忠实复现，验收）+ 图面区域版（可用画布，编辑）。
- **踩坑（已双写日志）**：`frame` 的 **y 轴是翻转的**——原点是 `(x0, y1)`，`y0` 是下边界。
  按直觉填 `23780,8160,29480,11670` 会让元素落到 canvas y 1510→4857（溢出画布高 3510）；
  正确写法 `23780,6760,29480,10210`。诊断捷径：先用只读 `plan` 试 frame 再落库。
- **TypeSafe 补的语义层**：导入按 P0 刻意 0 symbol / 0 connector，但图纸自带词表（12 图层 + 355 文字）。
  用 System One `jev-latest` 对 **112 个唯一标注 × 2 个独立判断**（`role` 是什么 / `subsystem` 属哪部分）
  + 12 图层，批 28、4 请求并发，**3.11 s / 95.9K in + 18.0K out**。
  41/112 两维都 ≥0.90 可直接采用；49/112 进人工复核队列。
  置信度是真信号：`尾气处理系统？` 0.31、`干净` 0.29、`预留接口` 0.32——模型在真不知道处说了不知道。
  产物 `reports/pid-repro/semantics_candidates.json` 是**旁挂候选**，**未写进文档**，P0 边界未越。
- **本轮交付物**：`reports/pid-repro/` 下 `复现报告.html`（主交付）、`final_drawing.png`（图面区域渲染）、
  `main_drawing.png`（全幅裁剪渲染）、`semantics_candidates.json`、`ts_semantics.py`（可复跑脚本）。
- **R8 追加 · Blender 3D 模型**：用 TypeSafe 对 71 个设备标注定**形体（15 类）+ 尺寸档（4 级）**
  （3 请求 / 1.83 s），经 blender-mcp 的 `127.0.0.1:9876` 裸 TCP socket 驱动 Blender 5.2，
  在**新建独立场景 `气路系统3D`**（`bpy.data.scenes.new()` + 手动 `coll.objects.link()`，不用 `bpy.ops`）
  建出 **116 台设备**（14 个参数化形体构建函数），按图纸真实坐标 × `SCALE=0.04 m/单位` 落位（厂区 197.7×114.9 m）。
  **用户既有场景 `Scene` 的 1019 个对象全程零改动**（收尾已把 `window.scene` 切回）。
  交付：`reports/pid-repro/气路系统_3D.blend`（1.4 MB）、`3D模型报告.html`、三张渲染图
  （`model_3d_iso.png` / `model_3d_iso_clean.png` / `model_3d_top.png`）、`blender_build.py` / `blender_drive.py`（可复跑）。
  能力边界已如实声明：**设备本体 + 真实坐标布局**，不含管线/连接关系（P&ID 语义未发明）。
  踩坑 5 条已双写日志（中文界面节点名 / `save_as_mainfile` 改路径 / 近垂直 TRACK_TO / 影子像变形 / `BLENDER_EEVEE`）。
- **待用户操作**：`~/.workbuddy-ai/mcp.json` 已写入 `blender` MCP 配置，需用户在连接器管理页右上角「自定义连接器」
  点「信任」后 MCP 工具才暴露；**本轮建模走直连 socket，未依赖该信任链**。
- **下一步（沿用 R7 未完成项）**：A5 汇报远端 → 签 Gate → push → 核对 CI 3.11 的 `core_corpus_digest`；
  随后 **B（F6 首答/注入契约）**。本轮未触碰该链路，无回归风险。

## 本轮收口（2026-09-22 R9c：**A5 Release Gate 已签并 push；post-push 验证全过**）

- **远端 reply22 正式签署 A5 Release Gate**，授权范围 **`1ba141c..5fc0964`**（f60693d / acd0d94 / 5fc0964），
  **明确排除 TypeSafe 提交**。按它的指定执行显式 push：`git push origin 5fc0964:main` → `1ba141c..5fc0964  main -> main`。
  `origin/main = 5fc0964`（本地 `main` 顶端仍是 TypeSafe `2270ab6`，`git merge-base --is-ancestor 2270ab6 origin/main` 为假）。
- **CI run `35702397038`（5fc0964）四 job 全 success**：M5 72-case gate / Backend 3.11 / Frontend Node 24 / Browser Chromium。
- **3.11 上的关键确认（从 CI 日志取）**：identity 复算步骤 exit 0（`identical: True`，`recorded 57` 定义身份 / 两文件 `recomputed 103`，
  missing/mismatched 均 none）；harness payload 里 `core_corpus_digest = 96b999fa…`；全量 pytest **884 passed**；
  acceptance `gate_failures []` + `safety_passed 13`；coverage/scale exit 0；qualification 按契约 exit 3。
- **Visual baselines run `35703003401`（5fc0964）= success**（手动 dispatch，因该 workflow 不跟 push 自动触发）。
- **A5 最终 golden（冻结）**：v3 `96b999fa90403c82b58018ef82b06bb190932d97a66f179ebde2c0e2ef41c10f`、
  归档 v2 `edb1c5d385bfd4f13a195119444dcb157f3bf0d893afd35e595ddc28c4b1c36c`（v2 定义为 A5 后派生的 archival 身份）。
- **下一个方向**：远端排期 **B — F6 首答 / 注入契约**；A6 qualification 继续是 non-blocking external dependency。

## 本轮补强（2026-09-22 R9d：**TypeSafe 真 key 跑通了，面板会说明"这次用谁的 key"**）

- **一个我上一轮报错的事实**：我说"本机没有 `TYPESAFE_API_KEY`"——错了。它就在 `~/.zshrc`（`export TYPESAFE_API_KEY=apikey_…`）。
  我的工具 shell 是**非交互式 bash**，从不 source `~/.zshrc`，所以我"看不到"它。这同时暴露了一个真实的界面缺陷：
  key 藏在 shell 配置里时，浏览器无法判断服务端有没有继承到它，于是"配了 key 界面却说没有"。
- **修复**：面板挂载时问一次 `/provider/typesafe/status`，在 Key 输入框上方写清**来源**：
  绿色"服务端环境变量已提供 Key（`TYPESAFE_API_KEY`）：下面留空就用它"／黄色"服务端环境变量里没有，请在下面填入"；
  输入框提示词同步改成"留空即用服务端环境变量的 Key"；另加一个「刷新服务端配置」按钮，`verify` 之后也自动刷新。
  逻辑放在 `describeTypesafeKeySource()`（`api.ts`，纯函数、可被 node 测试直接覆盖）。
- **真实调用（不再只有注入 transport）**：
  - `scripts/typesafe_live_acceptance.py`：真 key → `verify` `noul 0.97` 778 ms；一句"新增一台离心泵 P-201，把 T-101 接到 T-102"
    → 2 个判断（候选 7 符号 / 1 连接组合）907 ms、两处置信度 **1.00**，事务 `valid=True issues=none`，**exit 0**。
  - `frontend/scripts/typesafe-panel-check.mjs`（Playwright）：起真实服务端（key 只在服务端环境里，页面什么都不填），
    点 Agent 标签 → 展开「模型服务与高级设置」→ 断言来源提示是绿色且含 `TYPESAFE_API_KEY`、输入框提示为"留空…"，
    再点「测试 TypeSafe Key」→ 真答案 `Key 有效 · jev-1.13.0 · 749 ms`（服务端 `POST /provider/typesafe/verify 200`），无 console 错误。
  - 证据落 `reports/typesafe/`（`live-acceptance.txt`、`panel-key-source.png`）。本机 8000 端口是另一会话的服务，验收用 8123 + `/tmp/ts-ui.db`。
- **新增测试**：`frontend/tests/typesafeSettings.test.ts`（5 条：key 只进 `sessionStorage`、偏好里绝不含 key、
  三种来源提示、verify 的 key 在 body 不在 URL）；后端加 1 条"key 只在服务端环境里时 status 报 `api_key_source=environment`"的路由测试。
- **门禁（本地）**：`ruff` ✓；`pytest -q` **896 passed**；harness 9/9；acceptance **72/72**（六 family S@5 1.0、`gate_failures []`、safety 13/13、
  `core_corpus_digest 96b999fa…` 与冻结 v3 一致）；`repair-coverage` exit 0、`repair-scale` exit 0、`repair-qualification` exit 3（无凭据，按契约）；
  前端 `npm test` **149 passed** + build ✓。`reports/m5*` 一字节未改。
- **仍未 push**：本地 `2270ab6`（TypeSafe 主体）+ 本轮补强提交，远端明确要求排除 A5/F6 链。

## 下一步进展（2026-09-22 R9e：**B 项第一步——S@1 缺口定位为“全是 F6 夹具”，且这半个契约以前没人检查**）

- **定位（跑出来的，不是推断）**：acceptance 72 条逐条摊开，`S@1 = 0.8333` 的缺口**恰好是 12 条 F6**（0 条 F1–F5），
  三条 F6 operator 各 4 条，`required_attempts` = 2/3/5（`F6_CONTROL_FLOW`）。全部首轮 `compile_failed` 后收敛。
  即缺口 **100% 由冻结 spec 声明产生**，与 `docs/m5-agent-self-repair.md` 的记录一致。
- **真实缺陷在“量”的那侧**：`attempt_contract` 门只检查声明方向（声明 N 次 ⇒ 首次成功在第 N 次），
  “没声明重试 ⇒ 必须首轮成功”**从未被检查**（源码注释自己写着“no retry contract ⇒ no attempt claim”）。
  后果：F1–F5 里任何一条开始需要第二次尝试，只会让 S@1 变小，八项 gate 仍然全绿。
- **改法（测试层，不动冻结 spec/evidence）**：acceptance 回归测试现在钉三件事——① 无重试契约的 case 首轮必须成功；
  ② 只有 F6 能声明重试，且 `required_attempts` 必须等于 `F6_CONTROL_FLOW` 里该 operator 的值（“F6 变容易”会表现为 spec 变更）；
  ③ `S@1 == 无契约 case 数 / 总数`。反向断言能否变红由 synthetic case 单独证明。
  新提交 `39d8b75`（本地，未 push）。
- **结论（对远端 B 的回应）**：S@1 缺口里**没有 planner 缺陷可修**，它是控制流夹具；B 的剩下一步应当是把
  “减少 F6 首轮失败”换成“证明首轮失败只来自声明”，否则就是在拆掉 F6 存在的理由。

## 本轮追加（2026-09-22 R9b：**界面里配置 TypeSafe Key，并用 System One 判读画图**）

- **需求**：用户要求“在界面中的 agent 里面能支持配置 typesafe ai 的 api key，利用它来画图”。
- **做法**：新增 `backend/agentcad/typesafe.py`（凭据解析 request→环境、`/v1/systemone` 提交、`verify()`、状态；
  **从不把 key 写进日志/错误/响应**）与 `backend/agentcad/typesafe_planner.py`（判读式画图）。
  两个 POST 路由接在既有 semantic agent router 上：`/api/v2/provider/typesafe/verify`、
  `/api/v2/documents/{id}/agent/typesafe-plan`（返回与 `plan-v2` 相同的 `SemanticAgentPlanResult`，
  预览/应用走原有 apply-v2 + harness 闸门）。两者均已进 `surface_contract.py`（未登记写路径会被测试抦住）。
- **判读式画图的关键约束**：候选由代码给（符号目录按中文提示词组筛、已有设备两两组合取空闲端口）；
  模型只能在候选里选，选到候选外直接拒；置信度 < 0.34 的子句**跳过并写进说明**，不猜；
  位号/管径从用户原文正则取，几何由代码排（不占已有图形）。
- **前端**：Agent 面板内新增 TypeSafe 区块（开关 / Key / Base URL / 模型 / “测试 TypeSafe Key”）。
  key 存 **sessionStorage**（与既有服务令牌同一策略：刷新不丢、关浏览器失效、不落盘）；非机密的偏好进 localStorage。
  开启开关后本面板的生成请求改走 `typesafe-plan`。
- **文档**：`docs/typesafe-drawing.md`（界面字段、流程图、接口表、边界）+ README 一句。
- **测试**：`backend/tests/test_typesafe_drawing.py` **10 passed**（凭据优先级/缺失 400、不返回部分判断、
  子句分类、候选筛选、添加/连接建 op、低置信度跳过、候选外拒绝、路由挂载与缺 key 契约）。
  `pytest -q` **894 passed**（含 TypeSafe 与 A5 的 28 条），`ruff` ✓，前端 `npm test` **144 passed** + build ✓。
- **未验证部分（如实声明）**：本机环境没有 `TYPESAFE_API_KEY`，所以**没有做真实网络调用**；
  测试全部走注入 transport，请求形状与 `reports/pid-repro/ts_smoke.py` 已验证可用的调用一致：
  `POST https://api.typesafe.ai/v1/systemone`、`Authorization: Bearer …`、`model: jev-latest`、
  响应 `{"answers": {id: {choice, confidence, probabilities}}}`。用户一在面板里填 key，
  “测试 TypeSafe Key” 就会发出那一次真调用。

## 本轮状态（2026-09-22 R9：**A5 修正 —— 语料身份从“摘要”换成 corpus projection，等远端复查同一 Gate**）

- **远端 `reply20` 的裁定**：A5 架构、跨解释器、兼容性、历史 pin、CI ledger 全过；**唯一 blocker 是 identity
  强度不够**——它列的机械反例是“operator 数、code 集合、family/case 数全不变，但轮转 / base_variant / 声明参数 /
  producer 变化时 digest 不动”，因此 f60693d 那个东西的名字应该是 “stable corpus summary digest”。
  允许在同一本地提交上直接修正，**不要求 squash、不需要 spec v4 / corpus v4**，两个 golden 可以重算（它们从未发布）。
- **修正后的身份**：`core_corpus_digest(version)` 现在是 `core_corpus_projection(version)` 的 canonical SHA-256。
  projection 顶层：`corpus_id` / `corpus_version` / `spec_version` / `families` / `acceptance_cases_per_family` /
  `dev_cases_per_family` / `safety_case_count` / `case_derivation` / **`operator_catalogue`（23 行，含声明字段 +
  producer/base 定义身份）** / **`case_universe`（72 条）** / **`dev_case_universe`（24 条）**。
  每条 case 自带 `case_id` / `suite` / `family` / `index` / `operator_id` / `operator_declaration`。
  case 由 `generate_cases()` 真派生（不在这里重写轮转），seed **不进身份**（它随 candidate 变），身份记的是派生规则。
- **新 golden（本轮最终）**：v3 `96b999fa90403c82…`、归档 v2 `edb1c5d385bfd4f1…`
  （取代未发布的 `a60e07f1…` / `acb4a3bd…`，以及两个中间态 `115fe509…`/`a91461eb…`、`e23ce74c…`/`af6e5759…`；
  都未出版，按远端指示不保留为 legacy alias）。
- **远端点名的 `safety_case_universe` 已补，并按 reply21 拆干净字段**：投影里不再有裸名 `safety_case_count`，
  而是五个字段 —— `status`（projected / unavailable_archived）、`spec_declared_safety_case_count = 12`、
  `actual_safety_case_count = 13`（硬恒等于 `len(cases)`）、`cases`（13 行 case_id + title + builder 定义身份；
  归档为 `null` 而不是 `[]`）、`count_disposition = {declared: 12, actual: 13, status: frozen_spec_metadata_mismatch}`。
  两个数不一致是**被点名**的事实而不是被混用一个字段掩盖；下一次真正切 spec 版本时必须消掉。
  本轮 **未改冻结 spec 里那个 12**（改了会动 `spec_fingerprint`）。
- **本轮又拆出一个真坑（已双写日志）**：定义身份原先用 `inspect.getclosurevars` 决定“哪些名字是全局”，
  而这个集合随编译器变——3.12 内联推导式（PEP 709）后，同一个函数里的 `__name__` 在 3.11 被算作已解析全局、
  3.12 不算。AST 两边完全相同，所以这个差异**只有第二个解释器能暴露**（3.12 全绿、3.11 挂 5 条，dump 投影 diff 到 1 行）。
  修法：名字从 AST 取（`_referenced_names`），分类用 `co_freevars`/`__closure__`，值才查模块命名空间。
- **定义身份的覆盖面也扩了**：帮助函数按“它自己那个模块”折入（safety builder 的 `_add_layer` 之类在
  `repair_safety` 里），trace 键改为 `module:qualname`；复算脚本现在校验 **两个文件 101 条**定义身份。
- **实现那半边（声明式表达不了的部分）**：新增 `backend/agentcad/source_identity.py`（**零依赖**）把源码 AST 投成
  规范化形式再哈希；不能用 `ast.dump`——同一份 `_patch` 在 3.11 是 `930398d67e9220f4`、3.12 是 `a182c517faab2e95`
  （3.12 给 `FunctionDef` 加了 `type_params`）。规范化投影丢掉 `None`/空字段，所以新解释器新增字段只要为空就不影响身份。
  目录行发布 `producer_definition_identity` / `base_builder_definition_identity`；同模块被调函数递归折入、工厂
  绑定参数计入（`_build_f6` 的 `failures`/`base_operator_id`）。源码不可读时**报错而不是退化**（否则两台机器
  会“同意”一个什么都不标识的数）。
- **顺带清掉一个真重复**：`f3_delete_middle_detach` 原本用 inline lambda 重建 `three_valves` 底座，而
  `_three_valves_base` 的注释正是“命名而不是内联，免得两个 operator 漂成两张稍有不同的图”——现在它真的用那个函数，
  lambda 消失（也顺带修掉了匿名定义在身份里只能记成 `<lambda>` 的问题）。**行为不变**（同一 `build_base_drawing` 参数）。
- **双层测试（远端要求的两个方向）**：`tests/test_core_corpus_identity.py` **28 条**。
  **敏感性**：改 operator_id / 交换两个同 family operator 的 id（id 集合不变、只变归属）/ 改 defect code /
  改 base_variant / 改声明写入策略 / 换 producer 实现 / 增删 case / 增删 operator / 改 seed 派生规则 —— 9 条都必须移动 digest。
  **稳定性**：`generator_fingerprint` 变、`spec_fingerprint` 变、（本机+CI 两解释器）都必须不动。
- **跨解释器是实测的**：`/tmp/a5-311`（uv + CPython 3.11.15）与仓库 `.venv`（3.12.12）**各自跑完这 28 条全绿**，
  同一条 golden `96b999fa…` 两边都成立；`scripts/m5_closeout_identity_311_check.py`（标准库 + 零依赖的
  `source_identity`）在两个解释器上复算 digest 与 **101 条定义身份**（两个文件）全部相等。CI 的 M5 job 本身就是 3.11，
  已加一步跑这个脚本，所以这条性质由 CI 每天重跑而不是只在本地成立。
- **门禁（本地，全部在 `f60693d` + 本轮工作树上）**：`ruff` ✓；`pytest -q` **878 passed**；harness **9/9**；
  acceptance **72/72**（S@5 1.0、六 family 1.0、八门全 true、`gate_failures []`、safety 13/13、
  `core_corpus_digest 96b999fa…` 已进 payload）；`repair-coverage` exit 0；`repair-scale` exit 0；
  `repair-qualification` exit 3（无凭据，按设计）。**未 push**（远端明确要求修完先给它看）。

## 上一状态（2026-09-21 R7：**A5 corpus identity closeout 本地做完（未 commit、未 push），等远端签 Gate**）

- **基线**：`origin/main = 1ba141c`（R6 已 push 并验证，v3 冻结 anchor）；本轮按远端 `reply19` 的 A5 授权做。
  改动已落成本地提交（`feat(m5): give the frozen corpus an identity that does not move with the interpreter`，
  直接在 `1ba141c` 之上，单提交，5 个源码文件 + 2 个新测试文件 + 测试/CI/文档/证据），**未 push**。
- **A5 做了什么（identity-layer additive fix，不动语料）**：
  - **新增 `core_corpus_digest(version=None)`**：跨解释器恒定的语料身份 = manifest 去掉
    `CORPUS_IDENTITY_EXCLUDED_KEYS = (spec_fingerprint, generator_fingerprint)` 后取 canonical SHA-256。
    golden：v3 `a60e07f11d55…`、归档 v2 `acb4a3bde4ed…`（**A5 之后派生出来的 archival 身份**，v2 从未发布过该字段）。
  - `core_corpus_manifest("2")` 从冻结的 v2 spec body 重放（19 operator / 16 带 code / 72 case）；
    v2 manifest **没有** `generator_fingerprint`（v2 operator 字节码已随代码消失，缺席而非伪造）。
  - **`core_corpus_fingerprint()` 未改、未删**：仍是 runtime provenance（本机 3.12 `c85995d2…` /
    3.11 `82b37b04…`，允许不同）。
  - 结果 payload 新增 `corpus_version` + `core_corpus_digest`（**故意不发布** `core_corpus_fingerprint`）；
    两个名字同时进了 `REPAIR_SEMANTIC_EXCLUDED_FIELDS` 与 `REPAIR_LEGACY_HASH_EXCLUDES`。
  - `quality-harness` 的 self-repair contract 发布 `corpus_version`/`core_corpus_digest`，并新增断言：
    identity 输入里不得出现环境键（否则同一语料会在两个解释器上身份不同）。
  - **CI 的 M5 job 新增 `repair-coverage` 步骤**（exit 非 0 即红），报告进 evidence artifact。
  - **版本轴全未动**：spec_version 3 / corpus_version 3 / oracle_version 1 / payload version 1。
- **新增测试**：`tests/test_core_corpus_identity.py`（7 条：两个 golden、v2 重放、未知版本抛错、
  identity vs provenance 对比、payload 发布后两个哈希不变、冻结 v3/v2 evidence 仍可复算）；
  `tests/test_retired_extension_evidence.py`（5 条：extension 四个 pin + 四 case/三 unreachable 可读）。
- **本地门禁（全绿）**：`ruff` 全过；`pytest -q` **863 passed**（851 → +12）；harness **9/9**；
  **acceptance 72/72**（S@5 1.0、六 family 1.0、八门全 true、`gate_failures []`、safety 13/13、
  `evidence_verified true`）；`repair-coverage` **exit 0**（promoted 4/4 manifested、unreachable 3、
  `became_reachable []`、负向全红、`ledger_digest 30a50779…`）；`repair-scale` **exit 0**；
  `repair-qualification` **exit 3**（无凭据，non-blocking）。
- **跨解释器实测（不是断言）**：identity 的输入以纯 JSON 发布（`reports/m5-closeout/corpus-identity-inputs.json`），
  `scripts/m5_closeout_identity_311_check.py` 不 import 任何 `agentcad` 代码、只用标准库，
  在 **CPython 3.11.15 与 3.12.13** 上各跑一次，两个 corpus（v3 / v2）都得到与记录相同的 digest
  （输出：`reports/m5-closeout/corpus-identity-3.11.txt` / `-3.12.txt`）；push 后 CI 的 3.11 会再用真代码算一次。
- **pre/post-A5 兼容实测**：candidate `1ba141c` 的 **post-A5 live run** 与 pre-A5 的 CI 35579988999
  得到**同一个** `benchmark_semantic_hash`（`d719c89b…`）；冻结 v3 evidence 的 legacy `17be0e45…` /
  semantic `530e56f1…` 在“发布态”与“加上语料坐标”两种情况下复算一致（legacy 需先把两个哈希字段置空，
  与 runner 的做法一致）。
- **证据与脚本**：`reports/m5-closeout/**`（`corpus-identity.txt` / `.json`、`acceptance-closeout(-summary).json`、
  `coverage-closeout(-summary).json`、`scale-closeout.json`）由 `scripts/m5_closeout_identity.py` 与 CLI 生成；
  **`reports/m5/**` 与 `reports/m5-promotion/**` 一字节未改**。
- **下一步**：把 A5 汇报给远端（reply19 列的 1–7 项）→ 签 Gate 后 push → CI/Visual 上核对 3.11 的
  `core_corpus_digest` 与本机一致（应该一致：纯数据），并确认 CI 新的 `repair-coverage` 步骤绿；
  随后 **B（F6 首答/注入契约）**。

## 上一状态（2026-09-21 R6：**coverage-promotion 已 push 并验证（`origin/main = 1ba141c`），v3 冻结；远端已授权 A5**）

- **已推送并验证**：`origin/main = 1ba141c`（范围 `030f7d1..1ba141c`，2 个提交：`2e7e28f` 上一轮记账 +
  `1ba141c` promotion）。远端 reply17 先以「祖先 `2e7e28f` 未披露」暂缓签，披露范围后 reply18 **签了 push Gate**
  （并明确 v3 无需再等第二次 Gate）。往返原文：`.freebuff/remote-bridge/reply17.txt` / `reply18.txt`。
- **CI / Visual（都在 1ba141c 上）**：`CI 35579988999 success`（4 job：Frontend·Node 24 / Backend·Python 3.11 /
  M5 72-case gate / Browser acceptance·Chromium）；`Visual baselines 35580046077 success`。
- **3.11 上的数字**：pytest **851 passed**（163s）；harness 9/9（`spec_version "3"`）；acceptance **72/72**、
  invalid 0、governance 0、S@5 1.0、六 family 1.0、八门全 true、`gate_failures []`、safety 13/13、
  `evidence_verified true`；scale `passed_cases 5`；qualification 步骤断言 exit 3。
- **跨解释器语义**：candidate `1ba141c` 上本机 3.12 与 CI 3.11 的 `benchmark_semantic_hash` **相同**
  （`d719c89b…`）——上一轮的语义哈希修复在 v3 语料上继续成立。
- **身份核对（含两条口径更正，已写进 20-* 报告）**：`spec_fingerprint() = 8f522c75…` 双解释器一致；
  `spec_fingerprint("2") = c8520c5e…` 仍可复算；`ledger_digest = 30a50779…` 对 3.11 的 generator fingerprint **不变**（已验）；
  `generator_fingerprint` 3.12 `62503392…` / 3.11 `43979207…`；因此 **`core_corpus_fingerprint` 本机 `c85995d2…`、
  3.11 会是 `82b37b04…`**（它 manifest 里含 generator fingerprint，本来就是 RUNTIME）——这正是 A5 的目标，
  不是回归。**`repair-coverage` 不是 CI 步骤**：其断言由 `tests/test_repair_coverage_ledger.py` 在 3.11 的 851 里跑；
  push 后本机复跑 CLI **exit 0**（4/4 manifested、3 unreachable、`became_reachable []`、4 负向全红、
  `report_hash f3439950…` 可复现）。
- **远端 reply18 的授权（下一步）**：**A5 已批准** —— 新增 `core_corpus_digest`（stable / interpreter-independent，
  **不删除不重定义**现有 `core_corpus_fingerprint`，后者标为 runtime provenance），并按当前 v3 corpus 钉 golden；
  **extension pin test 也已批准**（不复活旧 generator，直接钉 immutable evidence：file sha256 `663461da…`、
  `coverage_corpus_digest 3dc8ca1a…`、`coverage_report_hash f56c2c50…`；若要钉 `corpus_fingerprint c198eb77…`，
  必须注明是 historical interpreter-specific identity）。**A6** 真实模型 qualification = non-blocking external
  credential dependency；**B（F6 首答/注入契约）**排在 A5 之后，**C** 更后。
- **reply19：A3/A4 通过，A5 scope 扩大并批准**（原文 `reply19.txt`）：
  1. `core_corpus_digest` = stable、interpreter-independent、**不含 `spec_fingerprint` 也不含
     `generator_fingerprint`**；`core_corpus_fingerprint` **保留**、不重定义、不删除，标为 legacy/runtime provenance。
     建议 helper 带版本参数：`core_corpus_digest()`（v3）与 `core_corpus_digest("2")`（归档 v2），两者都钉 golden；
     v2 那个要注明是「A5 后推导出的 archival stable identity」，**不是当年发布过的字段**。
  2. **不能引发版本漂移**：`spec_version 3 / corpus_version 3 / oracle_version 1 / payload version 1` 均不变
     （identity-layer additive fix）。
  3. **必须有 hash compatibility 测试**：把新字段发布进 payload 后，`benchmark_semantic_hash`（v1）与 legacy
     `benchmark_result_hash` **必须不变**；若该字段进了这两个 canonical payload，要作为 derived metadata 从
     digest 输入里排除。
  4. **extension pin test 加码**：钉四层（file sha256 `663461da…`、`coverage_corpus_digest 3dc8ca1a…`、
     `coverage_report_hash f56c2c50…`、**historical published `corpus_fingerprint c198eb77…`**），最后一条测试名/注释
     必须写明「historical interpreter-specific published identity; not the stable corpus identity」，且从 frozen JSON
     自校验，**不用当前 generator 重生**。
  5. **`repair-coverage` 加进现有 M5 deterministic CI job**（不新建 workflow）：exit 0、promoted manifested 4/4、
     unreachable 3/3、`became_reachable []`、负向对照按预期红。
  6. **不回写已冻结 evidence**：`reports/m5-promotion/acceptance-v3.json` / `disposition-ledger.json` /
     `reports/m5/repair-coverage-extension.json` 保持原样；A5 的新机器证据放**新路径**（如 `reports/m5-closeout/**`）。
  7. **A5 完成后的 Gate 汇报清单**：本地 SHA / range + diff stat；`core_corpus_digest` 的 v3 golden + v2 archival golden
     + 3.11/3.12 相同证明；`core_corpus_fingerprint` 允许不同但仍是 runtime provenance；pre/post-A5 的 hash compatibility；
     extension 四项 pin 全过；完整门禁（pytest / ruff / harness / acceptance 72-72 / repair-coverage CLI / scale /
     qualification exit 3）；并确认 spec v3、72-case corpus、已冻结 evidence 均未动。**A5 本地完成后先不要 push。**
- **本地状态**：本节 HANDOFF 更新 + bridge 记录（`20-push-verification.txt` / `20-chat-body.txt`）**尚未 commit**，
  按惯例留作随行记账提交。

## 上一状态（2026-09-21 R5：**coverage-promotion 已提交（spec v3 / corpus v3），等远端签 Gate**）

- **本轮执行的是 reply15 §③ 定下、reply16 仍未签的 `coverage-promotion`**。基线 `origin/main = 030f7d1a…`；
  改动当时落成本地提交（`HEAD` = 本轮的 `feat(m5): promote the four reachable coverage codes into the frozen corpus`，
  直接在 `2e7e28f` 之上）；**该提交随后已在 R6 获得远端签 Gate 并 push（`origin/main = 1ba141c`）。**
- **晋升结果**：4 个 representable code 进了**冻结目录** —— `f1_duplicate_label`(F1) /
  `f2_port_direction_mismatch`(F2) / `f4_unbridged_crossing`(F4, 底座 `three_valves+crossing`) /
  `f5_annotation_overlap`(F5)。**19 → 23 operator**，acceptance 仍是 12×6 = 72 case。
  **spec 从 2 切到 3**（`spec_fingerprint c8520c5e… → 8f522c75…`），**corpus 从 2 切到 3**
  （`core_corpus_fingerprint c4fb71fb… → c85995d2…`）。阈值 / oracle 1 / family / 布局 / safety 全未动。
- **晋升是检查不是记账**：`repair-coverage`（= disposition ledger）对每个晋升 code 从**冻结 generator** 里取出它那一条
  acceptance case（同 family / 同 candidate SHA / 同轮转），要求 canonical validator 确认 exact code 真的出现，
  再交给与冻结语料相同的 runner/oracle。当前 **4/4 covered**；晋升被回退或 operator 掉出轮转即失败。
- **3 个 unreachable 按远端正式名落库**：`SYMBOL_DEFINITION_MISSING` / `CONNECTOR_ENDPOINT_PORT_MISSING` →
  `unreachable_at_supported_ingress`；`CONNECTOR_ENDPOINT_POINT_MISMATCH` →
  `normalized_or_rejected_at_supported_ingress`（写请求 accepted 但被 `_normalize_endpoint` 重算，缺陷从不落地）。
  守卫测试保留：任一面哪天变得可达就先红、要求晋升。
- **本轮我做了主、已在报告里请远端裁定的三件事**：① **v2 spec body 归档**（`_SPEC_V2_OPERATORS` +
  `archived_operator_catalogue("2")`，于是 `spec_fingerprint("2")` 与 v2 的 case 集都仍可重现，被测试钉死；
  未知版本抛错）；② **退休 extension 语料**，其发布过的身份进 `SUPERSEDED_EXTENSION_IDENTITIES`（记录、不重算）；
  ③ 模块重命名 `repair_coverage_extension.py → repair_coverage_ledger.py`（CLI 命令名不变）+
  `MutationOperator` 新增显式 `base_variant`。
- **身份表（不需要第二个解释器就能证明）**：换掉 `generator_fingerprint` 后
  `spec_fingerprint("2")` / `spec_fingerprint()` / `ledger_digest()` **不动**；`core_corpus_fingerprint()` /
  `ledger_fingerprint()` / `generator_fingerprint()` 会动（provenance）。值：`ledger_digest = 30a50779…`、
  `ledger_fingerprint = 0b3e5e53…`、`generator_fingerprint = 62503392…`。
- **本地实测（本轮）**：`ruff` 全过；`pytest -q` **851 passed**；harness **9/9**；**v3 acceptance 退码 0**
  （72/72、S@5 1.0、六 family 1.0、八门全 true、safety 13/13、evidence_verified true）；`repair-coverage` 退码 0
  （4/4 + 3/3 + 负向全红）；`repair-scale` 退码 0；`repair-qualification` 退码 **3**（本机仍无凭据）。
  前端 `npm test` **144**、build ✓、Playwright **52 passed / 1 skipped**（视觉 10/10 未变）、shared **2 passed**、secrets ✓。
  （本机 8000 端口被另一会话占用，e2e/shared 用 `PID_AGENT_E2E_API_PORT=8123` + `PID_AGENT_E2E_PREVIEW_PORT=4390`。）
- **证据与报告**：`reports/m5-promotion/**`（spec 投影 / case 投影：39/72 条换了 operator / v3 acceptance /
  disposition ledger）；`scripts/m5_promotion_projection.py`；报告
  `.freebuff/remote-bridge/18-m5-coverage-promotion-report.md`。**`reports/m5/**` 一字节未改。**
- **未决（非本轮引入）**：真实模型 24-case qualification 仍缺凭据；`core_corpus_fingerprint` 至今把字节码摘要
  算进身份（建议下一版给它一条 `core_corpus_digest`），本轮**未做**。
- **当时的下一步（R6 已执行）**：等远端对 coverage-promotion 的裁定 → 批准后 push → 补 CI / Visual run id，并核对 3.11 上
  `ledger_digest` / `spec_fingerprint` 与本机一致（预期一致：纯数据、不含字节码）。

## 上一状态（2026-09-21 R4：**coverage-extension track 已落地（4 个 code 有 producer+证据，3 个证明不可达）**）

- **远端授权范围（reply13）已执行完**：`03a1764 → 1749e30 → 519ca23 → 631f1f8 → 9d53b69 → 22a030d`
  已 push；**新 HEAD = `22a030ab…`**。
  **`CI = run 35560585468 (success)`**、**`Visual baselines = run 35560614284 (success)`**
  （即远端要求的“push 后新 HEAD 的 CI run IDs”）。
- **本轮（coverage work）**：把“有策略、无 operator”的 7 个 code 分成两类结论：
  - **4 个能制造，已进 extension 语料**（`DUPLICATE_LABEL` / `PORT_DIRECTION_MISMATCH` /
    `UNBRIDGED_CROSSING` / `ANNOTATION_OVERLAP`）：每个都有 deterministic producer（受治理写入）、
    manifestation proof（canonical validator 读回、exact code 必须出现）、以及同一个 runner/oracle 的
    repair case；当前 **4/4 covered**，首答命中、单次 governed write。
  - **3 个任何受支持入口都造不出来**（`SYMBOL_DEFINITION_MISSING` / `CONNECTOR_ENDPOINT_PORT_MISSING` /
    `CONNECTOR_ENDPOINT_POINT_MISMATCH`）：写面与导入面都拒绝，且拒绝方式不同——前两个两面均
    hard-refuse；`POINT_MISMATCH` 的写请求**会成功但被 `_normalize_endpoint` 重算**，缺陷从不落地
    （只断言“没抛异常”的测试会误判）。结论：不是“漏测”，而是产品入口把它们挡在门外；规则与策略
    仍保留作纵深防御，`test_unreachable_codes_cannot_be_staged_by_any_supported_surface` 是这条断言的守卫。
- **corpus 边界（远端明确要求）**：extension 独立指纹
  `m5-coverage-extension / v1 / 4 case`（`coverage_fingerprint = c198eb77…`）；冻结语料另有一套
  `m5-core-corpus / v2 / 72 case / 19 operator`（`core_corpus_fingerprint = c4fb71fb…`）。
  **`BENCHMARK_SPEC_VERSION` 仍为 2，`spec_fingerprint` 未动（`c8520c5e…`）**；
  本轮重跑冻结 acceptance 仍是 **72/72**、S@5 = 1.0、八项 gate 全绿、safety 通过（与已发布 evidence 逐字段一致）。
- **负向能力**：`run_negative_controls()` 四个对照全部按预期变红（producer 只写不造缺陷 → `not_manifested`；
  声明错误 code → `not_manifested`；越 scope 的中性改动 → `locality_violation`；删 target → `deletion_not_permitted`）。
- **本地实测（本轮）**：`ruff check backend` ✓；`pytest -q` **811 passed**（新增 15 例）；
  quality harness ✓；`repair-coverage` 退出码 **0**（covered 4/4、unreachable 3/3、对照全红）。
- **本轮提交不是直接推 `12c2e87`**：远端 reply14 给了 Release Gate，但要求先证明它没有捎带上未被授权的祖先。
  实测 `86eaedb`（只改 HANDOFF 的记账提交）**是** `12c2e87` 的直接父提交，所以本地按远端要求
  「以 `origin/main = 22a030d` 为干净基线，只重放 coverage-extension 内容」重做了一个新 SHA。
  新 SHA 的树与 `12c2e87` **只差 `86eaedb` 那 12 行**（`git diff --stat 12c2e87 <new> = HANDOFF.md | 12 ---`），
  `86eaedb` 不在新 SHA 的 ancestry 里；它本身按远端要求继续隔离在本地分支 `backup-pre-m5-replay`（未推送）。
  三个指纹（`spec_fingerprint` / `core_corpus_fingerprint` / `coverage_fingerprint`）都未因这次重放而改变。
  **push 结果：`origin/main = 30a43393…`**；`spec_fingerprint = c8520c5e…`、`core_corpus_fingerprint = c4fb71fb…`、
  `coverage_fingerprint = c198eb77…` 三个值与远端记录一致，`repair-coverage` 的 `report_hash = f56c2c50…` 也一致。
- **CI 第一轮红了，原因不是功能而是“冻结指纹被解释器绑定”（已修）**：CI `Backend · Python 3.11` 的
  `test_extension_corpus_is_frozen` 断言失败：CI 算出 `073252f38b4a…`，本机（3.12）算出 `c198eb77…`。
  根因：`coverage_manifest()` 把 `generator_fingerprint`（**CPython 字节码**摘要）也放进被哈希的 payload。
  修法：新增 `coverage_corpus_digest()`（去掉 `ENVIRONMENT_DERIVED_KEYS` 后的 manifest 哈希，跨解释器恒定，
  测试钉死它）并保留原 `coverage_fingerprint()` 原值发布、按解释器记进 `PUBLISHED_FINGERPRINTS_BY_INTERPRETER`。
  另发现一条同类问题（已写进 `REUSE_AND_PITFALL_LOG.md`，未改代码）：`benchmark_result_hash` 的
  volatile 逃逸口只作用于顶层，同一 SHA 两次运行的哈希不同（细节待远端裁决）。
- **远端 reply15 裁决（已执行）**：`84d7399` = coverage-extension **Release Gate accepted**；`30a4339`
  的第一次 CI 红保留为已知失败历史点，不作 accepted anchor。⑥-1 **关闭**（按“跨解释器恒定语料身份 +
  运行时指纹”口径）；⑥-2 定为真缺陷，批准 **a′ 方案**：不动 M4 的 `canonical_digest`，在 repair 层新增
  `repair_semantic_digest()` / `benchmark_semantic_hash`（`REPAIR_SEMANTIC_HASH_VERSION = "1"`、递归剥
  `REPAIR_SEMANTIC_EXCLUDED_FIELDS`：volatile 字段 + 两个哈希 + `generator_fingerprint` +
  `semantic_hash_version`），legacy `benchmark_result_hash` **保留且规则冻结**（已发布的三份 acceptance
  payload 仍复算成当初发布的值）；`benchmark_result_hash` 待下一次正式 promotion 时降级为 deprecated。
- **semantic-hash 修复已 push**：远端签了 Release Gate，**`origin/main = 030f7d1a…`**（= `84d7399` +
  那个提交，fast-forward）。定性：M5 evidence determinism fix、repair-local、backward-compatible、
  不动 M4 canonical 语义、不动 M5 frozen v2 corpus/spec。
  **`CI = run 35571088506 (success)`**（四 job 全绿，Backend Python 3.11 的 `pytest` **840 passed**）；
  **`Visual baselines = run 35571130717 (success)`**。push 后最终校验记录：
  `.freebuff/remote-bridge/17-push-verification.txt`。
- **远端 reply16 的裁定（已执行）**：③-a 批准排除 `generator_fingerprint`（`spec_fingerprint` 保持在
  哈希内——它定义 benchmark 语义环境）；③-b 批准 `semantic_hash_version` 不进哈希、由验证器单独检查；
  legacy 兼容不变量批准（历史 payload + 新代码验证 = 原 legacy 哈希不变，不重写 `reports/m5/**`）；
  payload 顶层 `"version": 1` **保持 1**（additive backward-compatible，与 `BENCHMARK_SPEC_VERSION` 是两条
  独立版本轴）；**semantic golden = `2c8000e7…`（v1）**，`a208bdb0…` 只留作诊断历史。
- **本地未推送：只有一条 HANDOFF 记账提交**（本节），等下一次授权随行；除此之外工作区干净。
- **M5 spec v2 的判据记录不在本文件里，在 `docs/m5-agent-self-repair.md`**（`9d53b69` 已 push）：
  `S@1…S@4` 只发布不作硬门，硬门是 `attempt_contract` 与 `f6_s5_overall`；`BENCHMARK_SPEC_VERSION = 2`。

## 上一状态（2026-09-21 R3：**M4 regression fix 已获远端 Release Gate；schedule 小修已做完待授权推送**）

- **远端已正式 ACCEPTED `0600ee7`**（标签解析 regression fix 的 Release Gate）。`ecedc00` 继续是永久 M4 accepted anchor。
- **push 已执行（授权范围 = 7 个 SHA）**：`bccd807..9254b8e` → `origin/main`。
  **`M4 regression fix CI evidence = CI run 35557127990 (success, headSha 9254b8e)`**，四 job 全绿
  （Frontend 14s / Backend 3m40s / Browser acceptance 2m12s / **M5 self-repair 72-case gate 2m4s**）。
  **`Visual baselines = run 35557384183`**（手动 dispatch，绑定 9254b8e）。
- **`03a1764`（symbol.label consumer 审计证据）被远端排除在本次授权之外**，按它的要求隔离在本地未推送；
  下一次回传需补它的 scope（subject + files changed + stat + 是否改代码/测试/证据/语义）。
- **远端批准并已完成的 schedule 小修 = `1749e30`**（`fix(m4): resolve schedule tags after annotation polish`）：
  scope 仅 `_schedule_symbol` 的 tag 输出改走 `resolve_symbol_tag`；设备/仪表两条输出路径都覆盖；
  未动 `engineering_ir.py`、未重排 resolver 优先级、未重构 schedule。
  证据：`reports/m4-regression-fix/schedule-projection-{pre,post}.txt`（投影 diff = 每行只动 `tag` 字段：
  `""` → `"HV-101"` / `"PT-101"`）、§E2 仍未变（declarations 0 / unexpected 0）、M4 子集 **118 passed**、后端全量 **791 passed**、harness **9/9**。
- **登记为 consistency debt（远端明令本次不改）**：`engineering_ir.py:653` 的位号优先级是 `label` → `properties.tag`，
  与 canonical resolver 的顺序相反。polish 后 label 为空因而主流程无影响，但两者同时存在且冲突时
  IR 与 engineering-report 会给出不同 identity。
- **M5 R2 四项裁决（远端已下）**：
  1. **F6 的 S@k 保持标准 cumulative 定义，不得为提高 S@1 改 F6**；F6 的 gate 应同时检查
     “first-success attempt 与 case contract 一致”以及 F6 S@5 = 1.0；**全局 S@1 只作观测指标，不得作为 M5 硬拒绝门**。
     → 这需要改 gate/verifier 口径（当前 `THRESHOLDS.s1_overall` 是硬门），属待落地的下一步。
  2. 那 7 个“能修但考不到”的 code **不计入已验收能力**；可进入下一阶段 coverage work，
     每个 code 在宣称 supported 前必须有 deterministic issue producer + 制造证明 + 正式 repair case + acceptance 覆盖。
  3. **真实模型凭据不是 M5 Release Gate 前置条件**；真实 LLM lane 后续经 secret/env 注入，凭据不得进仓库/fixture/evidence。
  4. **`semantic_seed` 批准，但只允许存在于 benchmark construction / reproducibility 层**；不得进
     RepairRequest、planner context、canonical findings、repair hints、candidate generation；一旦泄漏给 planner 即判 case invalid / governance violation。
- **远端对“下一份回传”的要求**：push 后 CI run IDs/状态、schedule 小修 SHA + pre/post projection、若要推 `03a1764` 则补 scope。

## 上一轮状态（M5 candidate R2 @ `68fccb3`，数字仍有效）

- **本轮候选 = `68fccb3876934d690c0a8466821a6b24077a6818`**（`68fccb3`）。它**取代**上一轮的 `675af46`，理由：`675af46` 之后发现 M4 tag 规则读的是被 production polish 清空的字段（`0600ee7` 修），这使 `TAG_MISSING` / `TAG_DUPLICATE` 在 operator 目录里根本不存在——F1 只测到 line identity，而报告却称覆盖六个 family。`68fccb3` 补上两个 mutation（19 operator / 14 code）并加了一条对 `MUTATIONS` 全表参数化的守卫测试。
- **本轮实测（候选 `68fccb3`）**：`ruff check backend` ✓；`pytest -q` **785 passed**；quality harness **9/9**；acceptance **72/72**，S@1 **0.8333** / S@5 **1.0**，六 family S@5 各 1.0，safety **13/13**，governance violations 0，`evidence_verified: true`（`benchmark_result_hash = 8b657b3f…`，`spec_fingerprint = a69369c7…`，`generator_fingerprint = 13cc1254…`）；大图真实 **5/5** / 合成 **5/5**；前端 `npm test` **144 passed**、`npm run build` ✓、Playwright **52 passed / 1 skipped**、shared **2 passed**、`test:e2e:secrets` ✓；`repair-qualification` 退出码 **3**（`awaiting_real_model_qualification`）。
- **S@1 = 0.8333 的来源已查清**：F1–F5 各 12/12 一次成功，F6 **0/12**（故障注入要求第 2/3/5 次），因此缺口 100% 是 F6 的设计。已在 R2 报告 §3 提请远端裁定 F6 的 S@k 语义。
- **RSS 口径更正**：`10-` 报告写真实图“峰值 RSS ≈ 140 MB”，本轮同命令实测 **694–870 MB**（合成图 158–189 MB），140 MB 无法复现，以本轮为准。
- **报告**：`.freebuff/remote-bridge/11-M5-acceptance-report-r2.md`（取代 `10-` 的数字）；机器可读证据 `reports/m5/` 已按本轮候选全部重写。
- **未推送**：本地有 4 个未推送提交（`0600ee7`、`97a438e`、`68fccb3`、`ed5d399`），等远端批准；push 后按上一轮顺序补 CI run id。

## 上一轮状态（M5 candidate @ `675af46`，数字已被 R2 取代）

- **M5 开工基线**：远端 `REMOTE_BASELINE_2026-09-20_M5_START.md`，**`M5 start HEAD = f6738fc9a39573bb494b4e2c384ad79504bd7f6c`**（= M4 accepted HEAD 的记账提交，本地 HEAD 与之一致）。远端已给“连续执行 + 满足条件后 push”预授权。
- **M5 是什么**：Agent Self-Repair。一个 planner 契约、一个 orchestrator、一个 success oracle，两条轨道：Track D（deterministic，离线，CI 硬门）与 Track M（真实模型，release 证据）。实现落在 `backend/agentcad/repair_*.py`（15 个模块）与 `backend/agentcad/m4_invariance.py`。
- **契约与机械定义**：见 `docs/m5-agent-self-repair.md`。要点：
  - 失败候选**零写入**，最终最多**一次**受治理写入，带 undo/redo 证明；
  - scope 在元素存在前冻结；created id 由 create policy 判定并从 protected post 投影中排除；派生 scope 只能加宽；
  - 六个 family（F1 identity/F2 endpoint/F3 replacement/F4 routing/F5 collision/F6 replan），operator 目录进 spec fingerprint；
  - acceptance case 集由 `spec fingerprint + candidate SHA + family + index` 派生，不可手选；
  - `verify_benchmark_result()` 独立于 runner 重算 canonical hash、计数、S@1..S@5、per-family 率与 failure taxonomy。
- **M5-0…M5-6 已完成**：契约冻结、orchestrator（影子候选 + 局部性 + 原子受治理写入）、deterministic benchmark（dev 24 / acceptance 72）、safety-negative suite 13 例、真实模型 qualification 路径（无凭据时明确返回 `awaiting_real_model_qualification`，退出码 3）、REST/MCP/CLI/UI 四表面同源 + 审计绑定 + payload parity、§G scale track、§E M4 semantics invariance 机械门。
- **本地实测数字（本轮，未 push 前）**：backend `ruff check` ✓；`pytest -q` **750 passed**；offline quality harness **9/9**；deterministic dev suite **24/24**（S@5 = 1.0，六 family 各 1.0，safety **13/13**，governance violations 0）；前端 `npm test` **144 passed**；`npm run build` ✓；Playwright Chromium **52 passed / 1 skipped**（含视觉快照未变）；shared-mode security acceptance **2 passed**；`test:e2e:secrets` ✓。
- **大图 scale track（§G）**：真实图 `.freebuff/repro/source.dwg`（939,381 B / AC1032 / **不在 git 里**），SHA-256 `5e62ec5c…`，导入 9757 elements / 13.6 s，base validation 420 ms；固定 5 case（F1/F2/F4/F5/F6）**5/5 通过**，governance violations 0，context bytes 6.5–9.1 KB（即 9772 元素的图上工作仍然局部），shadow validation ≈ 3 s，单 case wall-clock ≈ 17 s。CI 跑合成大图（几百 elements）。
  - **必须记住的事实**：真实 CAD 文件导入后是 7167 line + 1830 polyline + 398 circle + 362 text，**0 symbol / 0 connector**。修复 case 需要一个带 port 的元素，所以 working copy 会先通过 semantic compiler + 受治理事务追加一条阀组 train，报告里 `semantic_seed` 字段写明这件事；`--no-semantic-seed` 则拒绝并返回退出码 3。
- **真实模型资格状态**：本机无 model provider 凭据（`PID_AGENT_LLM_BASE_URL` / `PID_AGENT_LLM_MODEL` 未设，本地 Ollama 无模型）。因此 M5 最终状态**只能是 `M5 candidate / awaiting real-model qualification`**，本地不得自行签 accepted。
- **M5 候选与 CI（已完成的部分）**：
  - **`M5 candidate / evidence SHA = 675af468c9622607e2890dac941e293f3113d853`**（`675af46`）；
  - **`M5 CI evidence = CI run 35549650801 (success)`**（四个 job：Backend · Frontend · Browser acceptance · M5 self-repair，全部绿；M5 job 内新增的 scale track 5/5 与 qualification 退码 3 断言均已实跑）；
  - **`M5 Visual baselines = run 35549826268 (success)`**（手动 dispatch，绑定同一 SHA；workflow 仍为 `ubuntu-24.04` + renderer sentinel + update 不吞错 + sentinel recreation + assert rerun）；
  - **`M5 accepted HEAD = pending`**（只能由远端 final Release Gate 命名，本地不得自行填写）；
  - 记账口径：`675af46` 是“改了什么 + CI 测过什么”，后续只允许出现记账/文档提交；若记账提交产生新 SHA，**不得**把它写成 accepted HEAD。
- **验收报告**：`.freebuff/remote-bridge/10-M5-acceptance-report.md`（本地，交付给远端）；已提交的机器可读证据在 `reports/m5/`（acceptance 72 例全文 + 大图真实/合成 + qualification）。
- **未决项（唯一 acceptance blocker）**：本机无真实模型凭据（`~/.ollama/models` 为空、无 `.env`、无 keychain 条目），因此按 §A4 只能记 `awaiting real-model qualification`。`reports/deepseek-v4-flash.json` / `reports/ollama-qwen.json` 是早期 `model-matrix` 报告，**不能**当 M5 的 24-case qualification 证据。
- **待办**：远 final code review → 远端命名 M5 accepted HEAD；凭据可用时补 24-case 真实模型 qualification。

## 上一轮状态（M4 —— Engineering Validation System，**已由远端 Release Gate 正式 ACCEPTED @ `ecedc00`**）

- **M4 验收口径（远端 `REMOTE_BASELINE_2026-09-19_M4_ACCEPTED.md` 正式签发）**：
  - **`M4 accepted HEAD = ecedc00ae3063a4043334bd30367008d00665a29`**（`ecedc00`）
  - **`M4 acceptance-fix content commit = b5f2ca5`**（R3 §2 必修项的内容提交）
  - **`M4 CI evidence = CI run 35421802528 (success) + Visual baselines run 35421823171 (success)`，两者均绑定 `ecedc00`**
  - 两者含义不同、必须同时保留：`b5f2ca5` 是“改了什么”，`ecedc00` 是“被接受的仓库状态”。**不得把 `b5f2ca5` 写成 accepted HEAD**；也不得因为写 SHA 的记账提交而产生新 SHA 就改口 accepted HEAD——**M4 accepted SHA 永远固定为 `ecedc00`**。
  - 远端代码复查结论：R3 mandatory items 已全部成立，未再发现 acceptance blocker；唯一残留为非阻断的 P2 清理项（`test_the_published_hash_can_be_recomputed_from_the_published_payload` 末尾有一处恒真自比较），**M4 accepted 后顺手删，不得为它重开 M4**。
  - 下一阶段：完成 accepted-SHA 记账提交后直接进入下一大阶段；除发现会推翻 M4 acceptance invariant 的真实 regression，否则不再回开 M4。
- **上一轮状态（以下保留为历史）**：远端评审 Round 2 + M4 Engineering Validation System 实现完成、待远端验收。

- **里程碑口径**：**M4 — Engineering Validation System 已获远端 Reviewer / Release Gate 正式授权**，Charter 完成条件引用为 **§49**（`§48` 是 M3，不得再混用）。执行顺序由远端基线强制：`M4-0 CAD/CI gate closure` → `M4-1` canonical validation contract + project profile → `M4-2` 既有 validator 适配 → `M4-3` 可配置规则引擎与 profile 解析 → `M4-4` release validator → `M4-5` REST/MCP/CLI/UI/审计表面 → `M4-6` 全量验收与里程碑门禁。
- **M4 状态（远端基线 `REMOTE_BASELINE_2026-09-19_M4_GATE_R2.md` 规定的口径）**：M4-0 原九项 CAD/CI gate 已 fix-forward 通过，锚点为 **`bcacd8c`**，不做 CAD 回退。**`bcacd8c` 是 M4-0 fix-forward anchor，不是整个 M4 的 accepted HEAD。** M4-1/2/3 实现 canonical 校验契约、legacy adapter、project profile 解析、waiver 与 readiness 核心，必须满足 R2 的 Round-2 必修项（见该基线 §2/§3）。M4-4/5/6 待完成。**最终 M4 accepted SHA 与实测验收数字只在 M4-6 之后填写**，M4-1/2/3 的修复提交只记为 `M4-1/2/3 implementation anchor`。
- **远端评审（2026-09-19）**：评审基线 `main@2389ee7`，评审切片 `d5c1fa0..2389ee7`（CAD 图纸导入切片）。裁决 = **CONDITIONALLY ACCEPTED，fix-forward，不整体回退**。架构方向保留（几何复现不造工程语义、只新建文档、只走 `DocumentService` 受治理通道、DWG 解码只用 subprocess 外部工具、转换器选择带证据、重命名控件是真 bug 修复、平台化视觉基线被接受）。
- **M4-0 必修项（远端指定，全部已实现，见提交）**：① AutoCAD 脚本路径注入（用户文件名不得进入命令行/`.scr`，改为固定内部名 `source.dwg` / `output.dxf` / `converter.scr`）；② 导入必须是**一次**逻辑受治理变更（一个 revision、一条历史、一条审计、一次 undo），不得留下可用的半成品图；③ 源 SHA-256 必须同时进入**审计证据**（不只文档 metadata）；④ 视觉基线 workflow 不得用 `|| true` 吞掉浏览器/服务/测试失败，并把 Linux 渲染器钉到 `ubuntu-24.04`；⑤ 转换器非零退出码默认判失败（`acceptable_exit_codes` 默认 `(0,)`）；⑥ AutoCAD 版本探测改为真正的墙钟超时（静默进程也不会卡死）；⑦ 非均匀缩放块参照里的圆必须保几何（采样为闭合折线 + `CAD_CIRCLE_APPROXIMATED`）；⑧ CLI/MCP dry-run 与真导入共用读取入口，缺文件返回稳定 `CadImportError`；⑨ 文档契约修正（`§48→§49`、`cad_block`、`cad_import`、`dwg2dfx→dwg2dxf`、去掉“分批事务即 undo 单位”的说法、转换器 verified 只代表该工具族在参考语料上跑过、许可改为事实性打包口径、公开报告不暴露本机路径）。
- **CAD 切片的既定口径（远端确认保留）**：导入只是**几何复现**；块出处键为 `cad_block`（元素级来源为 `cad_source`，文档级为 `cad_import` metadata）；一次完成的 CAD 导入 = **一个逻辑受治理变更 / 一次 undo**，不做“分批事务”的中间态；正式 release 只产生**就绪证据**，Agent 可以请求审批但绝不能自批 `Approved`/`IFC`/`AFC`/release 状态。
- **M4-0 追加 hardening（Round 2 评审发现，已完成）**：① 受治理写入的**共享 mutation kernel**——`apply_transaction` 与 CAD 批量建文档都走 `DocumentService._stage_mutation()`，operation 应用/editor-group 归一/版本号约定/结果文档校验只有一份实现，并有 parity 测试断言两条路径产出同一文档、同一审计证据键集、同一次逻辑变更；不是“声明”，是代码事实（也不再声称存在有界分块写入）；② 审计证据**保留键**保护——调用方只能新增证据（CAD 走 `cad_import` 命名空间），覆盖 `RESERVED_EVIDENCE_KEYS`（`change_count`/`validation`/`action` 等）会抛 `ReservedEvidenceError`，写入前失败；③ 视觉基线 workflow 的“没有产出基线”判定改为**更新步骤前的 marker**（`find -newer marker`），不再用相对时间窗（fresh checkout 会让旧 PNG 看起来是刚生成的）。
- **M4 实现锚点（按远端 R2 要求的次序提交）**：`bcacd8c` = M4-0 fix-forward anchor；`a1e7b47` = **M4-1/2/3 implementation anchor**（canonical 契约 + profile 链 + readiness 核心，含 R2 Round-2 必修项）；`f20b4fc` = M4-0 追加 hardening（共享 mutation kernel + 审计保留键 + 基线 marker）；`af5dfa0` = M4-4/5（REST/MCP/CLI/UI 读表面 + 审计 read 事件 + harness `validation_contract`）。**这些都不是 M4 accepted HEAD**：M4 accepted HEAD 只能在远端审阅 M4-6 验收报告后填写。
- **Round-3 fix-forward（`b5f2ca5`）本地重跑数字（2026-09-19）**：backend ruff ✓；pytest **678 passed**（`25b60c6` 的 657 + 21，含新增 `tests/test_validation_parity.py` 三方 payload 等价测试与 waiver/时间/policy/harness 绑定测试）；offline quality harness **8/8**；前端 `npm test` **144 passed**（141 + 3 条绑定用例）；`npm run build` ✓；Playwright Chromium **52 passed / 1 skipped**；shared-mode security acceptance **2 passed**（注：跑 shared 前必须先 `npm run build:e2e`，生产构建不含 e2e 钩子）；视觉快照 **10/10 未变**。R3 明确本类契约/表面修复不必重跑真实 DWG 基准，故未重测性能。
- **M4 验收编号（本地实测，2026-09-19，`25b60c6` 口径）**：backend ruff ✓；pytest **657 passed**；offline quality harness **8/8**（新增 `validation_contract`）；前端 `npm test` **141 passed**（M3 基线 134，+7）；`npm run build` ✓；Playwright Chromium **52 passed / 1 skipped**（本机装了 AutoCAD，能力分支跳过）；shared-mode security acceptance **2 passed**；视觉快照 **10/10 未变**（新增校验面板只在「报表/检查」tab 内渲染，不参与截图集，因此 darwin/linux 两套基线**都无需重生成**）。
- **代表性大图性能（本地方法：导入 1 次 + warm-up 1 次 + 测量 5 次）**：真实图纸「气路系统总图.dwg」939,381 B / AC1032 / 最终元素 **9757** / 导入操作 9769 / 逻辑变更 1 次；导入（converter=autocad-core-console）7.4 s（**不计入校验时间**）；canonical 校验 cold **0.758 s**，5 次 **0.751 / 0.696 / 0.659 / 0.769 / 0.700 s**，median **0.700 s**，min 0.659 / max 0.769，峰值 RSS ≈ 303 MB；结论 3 个 error、0 个 blocker。复现入口：`scripts/m4_validation_perf.py`。M4 **不设**跨机器硬阈值。
- **确定性证据**：真实图纸上固定 `as_of` 连跑 3 次 → 同一 `result_hash`、逐条 issue 相同；改变 `as_of` → 哈希变化（时间是被绑定的输入）；校验前后 revision（1）与历史（1 条 create）不变 → 校验确为只读。
- **M4 Round-3 裁决（远端基线 `REMOTE_BASELINE_2026-09-19_M4_GATE_R3.md`，2026-09-19）**：架构继续接受，不回滚、不重写前五个提交；`25b60c6` 被命名为 **M4 Round-3 candidate / review anchor**，**不是 accepted HEAD**。R3 §2 的必修项（waiver 时间下界、engine 时间契约、删掉 analyzer 外层 broad catch、canonical serializer + 跨面等价测试、UI 单评估时刻与哈希绑定、harness 按 validator 绑定 rule id、release policy 规范化、profile 路径不泄露、文档事实修正）已全部落地，见本地仓库约定：
  - `M4-0 fix-forward anchor = bcacd8c`
  - `M4-0 Round-2 hardening anchor = f20b4fc`
  - `M4-1/2/3 implementation anchor = a1e7b47`
  - `M4-4/5 implementation anchor = af5dfa0`
  - `M4 Round-3 candidate/review anchor = 25b60c6`
  - `M4 acceptance-fix candidate = b5f2ca5`（Round-3 fix-forward，关闭 R3 §2 全部必修项）
  - `M4 accepted HEAD = pending final pushed CI-green candidate`（只能由远端 Gate 填写，本地不得自行填写）
- **Round-3 语义修正（已进代码与文档）**：① waiver 生效区间自 `granted_at` 开始——历史 `as_of` 早于 `granted_at` 的 waiver **不生效且不得标成 `expired`**（`expired` 只表示“已批准但已失效”）；② 时间契约由 engine 拥有：`run_validation` 拒绝 naive 时间（稳定码 `as_of_not_timezone_aware`）并把时刻规范化为 UTC，因此同一瞬间的两种写法得到同一 `result_hash`；③ `ValidationContext` 不再整段捕获 `(KeyError, ValueError)`——只有显式分类的 typed `ValidationContextUnavailable` 才变成 skip，analyzer 内部的编程错误直接抛出（并有直接 patch analyzer 的测试）；④ REST/CLI/MCP 由唯一的 `canonical_payload()` 序列化，`schema` 是 canonical 公开字段名（内部名 `schema_name` 不得出现在机读 payload 里），并有完整 payload 等价测试 `tests/test_validation_parity.py`；⑤ UI 每次刷新只生成一个评估时刻并同时发给 validation/readiness，展示前校验 revision/profile/规则包/`evaluated_at` 与 `readiness.validation_hash == result.result_hash`，不一致就显示不一致状态；⑥ harness 按 `(validator_id, code)` 绑定规则身份（错 validator 发出已知 code 会失败）；⑦ 生效 release policy 规范化（去重 + validator id 升序 / severity 顺序），fingerprint、判定、发布值与 readiness hash 一致；⑧ profile 公开 `source` 为逻辑身份（`built-in` / `file:<basename>`），不泄露服务器路径；⑨ 文档事实修正（不再声称批量建文档“复用 `apply_transaction` 同一套路径”、也不再有“分块写入”的说法，见 `REUSE_AND_PITFALL_LOG.md`、`docs/cad-import.md`、`cad_import.py`）。
- **Round-3 推送与 CI 权限**：远端已**预先授权**修完 R3 §2 且本地全量套件全绿后直接 push 到 `origin/main`（不需再申请许可），但**不得**把 `25b60c6` 当最终候选推送。M4 accepted HEAD 只能在“推送后拿到真实 CI（含手动 dispatch 一次 `Visual baselines`）全绿”之后由远端命名。
- **本轮（M4）状态**：完整验收报告见对话中交付的 M4-6 验收报告 + `05-m4-acceptance-report.md`；accepted HEAD 仍为 pending。

## 上一轮状态（DWG/DXF 图纸导入切片，`d5c1fa0..2389ee7`）

（远端评审基线；评审结论见上：conditional accepted / fix-forward）

### 当轮记录

- **里程碑口径（先看这条）**：**M3 已由外部架构验收正式签字**（accepted HEAD `d5c1fa0`，功能基线 `b47f197`）。本轮**不是 M4**：`M4 — Engineering Validation System`（§48 / Priority 2 Validator Framework）**未开始也未经验收**，不得因为本切片动工。本切片是用户直接要求的产品能力——把外部 CAD 图纸接进项目（Charter §14 图纸接入），按实现优先级单独交付，不改变 milestone 编号。
- **本轮交付（DWG/DXF 导入，`docs/cad-import.md`）**：
  1. **自研读取层，零运行时依赖**：`cad_dxf.py`（group-code DXF 读取器：块展开、bulge、镜像/嵌套/阵列插入、HATCH、SOLID、MTEXT、图层/颜色/线型、编码页）与 `cad_dwg.py`（LibreDWG 对象流展开）。`ezdxf` 仍是**仅供测试**的交叉校验依赖，运行时不引入。
  2. **转换器适配层**（`cad_convert.py`）：只以 **subprocess** 调用已安装的外部工具，不链接、不 vendor；每条候选都带 `evidence`（`verified`/`unverified`）与失败尝试记录。发现顺序按实测保真度：**AutoCAD Core Console → LibreDWG 对象流 → ODA → LibreDWG DXF**。
  3. **落地走唯一受治理写通道**：导入创建**一个新文档**，图层/图元按 `chunk_size` 分批走 `POST /documents/{id}/transactions`，因此 revision 校验、历史、语义 diff、审计、undo 全部继承；**不能修改/重新版本化/删除任何既有文档**（写进 `surface_contract.DRAFT_EDIT_EXCEPTIONS` 的理由里）。文档 metadata 记录 `cad_import`（文件名、格式、SHA-256、转换器、帧、统计）。
  4. **导入报告是唯一诚实凭据**：源指纹、解码器与完整命令行、各类图元计数、图层、源/导入范围、画布、**逐条“未能复现”错误码与数量**、事务/revision/耗时、警告。
  5. **接口面**：REST `GET /imports/cad/capabilities`（只读）、`POST /imports/cad/plan`（dry run，登记为 **read**）、`POST /imports/cad`（写，tool `import_cad_drawing`，audited）；MCP `import_cad_drawing`；CLI `pid-agent import-cad`；前端左侧「导入 DWG/DXF」「仅解析图纸」+ 只读导入报告面板。
  6. **不造工程语义**：符号只留 `dwg_block` 块名元数据，不猜 `gate_valve`/`ball_valve` 之类（P0）。导入结果是**几何复现**，不是设备/管线图。
- **真实图纸实测（用户提供的 `气路系统总图.dwg`，917 KB / AC1032 / 9283 实体）**：三条路线用**同一个**读取器解析对齐：

  | 路线 | 原生图元 | 丢失块定义 | 耗时 |
  |---|---|---|---|
  | **AutoCAD Core Console → DXF**（官方引擎） | **9757** | **0** | **3.7 s** |
  | LibreDWG `dwgread -O JSON` 对象流 | 9242 | 0 | — |
  | LibreDWG `dwg2dxf` → DXF | 6523 | **157** | — |

  157 个丢失块定义就是真实图纸上最显眼的 140 个阀门（`PDS2D-6Q1C15`）与增压泵/止回阀/风机（动态块在 LibreDWG 的 DXF 写出器里是空的）。**AutoCAD 2027 for Mac 自带无界面引擎** `AcCoreConsole`（`AutoCAD 2027.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole`），本轮已接入并测通。
- **关于“官方 MCP”（用户直接问过）**：Autodesk 官方公开的 MCP server 是 **Revit / Model Data Explorer / Fusion Data / Product Help** 与 ACC/Forma 平台侧服务，**没有 AutoCAD 桌面版官方 MCP**；市面上的 AutoCAD MCP 都是第三方且依赖 Windows 的 COM/.NET/文件 IPC，在 macOS 上无法驱动 AutoCAD。而且即使可用，**也不该接**：本项目所有写入必须走唯一受治理通道（Charter §7 / P0-2），一个直接改 DWG 的外部 MCP 会引入第二条无审计写路径。正确接法是本轮的实现——把 AutoCAD 当**解码器/旁证**（`accoreconsole` + `DXFOUT`），而不是写手。
- **本切片验证（本地）**：Ruff ✓（全仓库 `All checks passed`）；pytest **580 passed**（M3 基线 437，本切片 +143，其中 CAD 专项 142）；offline quality harness **7/7**（新增 `cad_import_contract`）；前端 `npm test` **134 passed**（M3 基线 117，+17）、`npm run build` ✓；Playwright 本地全量 **0 failed**（新增 `cad-import.spec.ts` 3 通过 + 1 条件跳过：本机装了 AutoCAD，缺解码器那条路径按能力探测跳过；视觉快照 10/10 通过）。
- **顺手修掉的两个存量问题（都不是 CAD 功能引入的）**：
  1. **重命名控件从产品里消失了**（真实 bug，不是测试陈旧）：`RuntimeEnhancements` 用 `documents-panel > .panel-heading` 子选择器找 portal 目标，而左侧面板在 `61cc572`（2026-08-21）长出了 `.left-panel-section` 包装层，选择器不再匹配 → portal 目标为 null → 按钮从未渲染；这正是重命名 e2e “自基线以来就失败” 的真因（它一直在报一个缺失的产品控件）。改为后代选择器后控件恢复，e2e 同时显式 opt-in（`sessionStorage["pid-agent:enable-runtime-e2e"]`，运行增强在 E2E bundle 里默认关闭）。
  2. **视觉基线跨渲染器债务清零**：截图基线原先在 macOS 生成、CI 在 Linux 断言，字体光栅化差异约 2%（阈值 1.5%），使 Browser job 自 M2 之前就持续红——而红的 Browser job 会 **skip 掉 shared-mode security acceptance**，等于一个安全门被字体差异默默关掉。现在 `snapshotPathTemplate` 带平台后缀（`{arg}-{platform}{ext}`），**每个平台各一套基线**（darwin/linux 各 10 张），CI 与 macOS 各自与自己渲染的像素比对；Linux 那套由新增的手动 workflow `.github/workflows/visual-baselines.yml` 在 CI 渲染器里重生成（它是“更新并上传产物、不自动提交”，提交仍由人审阅）。提交前用 CI 自己抓的 `*-actual.png` 做过验证：重生成的基线与 CI 实际渲染差异 **0.0003–0.0011**（阈值 0.015）。
- **提交与推送状态（本切片）**：`0f1b9f6` 后端（读取层/转换器适配/受治理落地/接口/测试）→ `024624d` 前端（导入面板/API 客户端/报告 UI/单测/e2e）→ `0658bca` 文档（cad-import、README、tool-registry、HANDOFF、pitfall log、`.gitignore`）→ `b957d92` 修复（重命名控件 + e2e opt-in + 基线 workflow）→ `d4dbdb1` Linux 基线重生成 → `5443be7` 每平台一套基线。
- **CI 真实结果（本切片）**：run `35382453510`（`0658bca`）Backend ✅ / Frontend ✅ / Browser ❌ **42 passed / 10 failed**（1 个重命名定位 + 9 张视觉快照；相对 M3 基线新增的 1 张已定位为导入控件改动引起的真实像素变化）→ run `35383880680` 起 **全绿**：Backend ✅（pytest **580 passed**、harness **7/7**、ruff ✓）、Frontend ✅（**134 passed** + build）、Browser ✅ **local-mode 52 passed / 0 failed** + **shared-mode security acceptance 2 passed**（不再被 skip）→ run `35387424366`（`5443be7`）同样三项全绿。
- **本切片已知边界（不藏）**：① `issues` 里明确记账，不假装做到：图案填充跳过、样条跳过、布局空间跳过、旋转文字只保锚点、填充以多边形表达（agentcad 无填充图元）、弧/椭圆采样为折线（`curve_segments` 可调）；② 符号语义映射表仍需人工确认；③ 大图勿整图跑整理引擎（M3 已知债务）。
- **本切片新增债务/注意**：① 截图基线现在是**每平台两套**，新增 UI 后必须两个渲染器各自重生成一次（macOS 本地 `--update-snapshots`，Linux 用 `Visual baselines` workflow），改 UI 而只更新一套会让另一套失真；② 本地跑 e2e 前必须先 `npm run build:e2e`，否则 `npm run build` 留下的生产 bundle 没有测试 bridge，所有截图场景都会超时（本轮踩过）；③ AutoCAD 解码依赖用户已安装并授权的 AutoCAD，本机路径写进 `EXECUTABLE_GLOBS`，CI 上没有它——因此 CI 上走的是 LibreDWG/“无解码器”分支，新增测试必须把 `EXECUTABLE_GLOBS` 置空以免开发机装了什么就改变测试结果（已固化）。
- **工程债/待办**：`.freebuff/` 已加入 `.gitignore`（本地临时工作区：运行文档、提交信息草稿、一次性复现脚本，均非产品代码）。

## 上一轮状态（M3 —— Deterministic Drafting Engine，含验收后单调性窄补丁）

- **里程碑口径（先看这条）**：Charter 的长期 milestone 是 `M0 Structured Editor → M1 Tool Harness → M2 Engineering Semantic Graph → M3 Deterministic Drafting Engine → M4 Engineering Validation System → M5 Agent Self-Repair …`。**M2 已由外部架构验收正式签字，accepted baseline = `49e3e14`**；本轮做的是 **M3**。`Priority 2 Validator Framework` 仍是**实现优先级**，不得改口称为 M3，也不得在 M2 与 M3 之间新造阶段；`Priority 3 Deterministic Drafting`（§34）与 `M3`（§48）是同一件事的两种编号。**M3 完成即停止开发，未经外部验收不得进入 M4。**
- **M3 本轮交付（Deterministic Drafting Engine）**：
  1. **一条确定性流水线**（`drafting_engine.py`）：锁定解析 → 区域排布（仅第 1 轮）→ 端口感知重路由 → 标签摆放 → 跨线桥接 → 保留空间驱逐 → 碰撞松弛，循环到不动点（`pipeline_rounds`，默认 3，有限且可复现）；每个阶段只有**不使任何硬性图面指标变差**时才被接受（`DRAFTING_HARD_FIELDS`），否则整阶段回滚并记 `DRAFT_STAGE_ROLLED_BACK`。
     **参照点是“上一个已被接受的状态”**（阶段局部单调，引擎 v2，见下方验收后补丁条）：pass 之间互相干扰（挪设备会作废原先最优的走线），所以“最终比输入好”不等于“没有阶段把上一阶段的成果吐回去”；`DRAFT_RESULT_REGRESSION`（最终 vs 输入）是第二层保险，不是全部保证。
  2. **只读引擎**：`preview` 只返回一份 `TransactionRequest`，永不写库；落地只能走唯一受治理写通道（网页 `transact` / MCP `apply_deterministic_drafting` / 人工确认），因此权限、revision 校验、审计与 undo 一个都不少（Charter §7 / P0-2）。REST 两个整理路由在 `surface_contract.py` 里登记为 **read**——“整理没有私有写路径”是机器可检查的。
  3. **手动锁成为一等数据**：三种来源合成一个冻结集并逐条回报来源——`request.locked_element_ids`、`element.metadata["drafting_lock"]`（编辑器钉住、跨会话）、`metadata.layout_regions` 中 `kind="lock"` 的区域。同一条锁定语义也接入了 `auto_layout`，排布引擎与整理引擎对“什么算锁定”只有一种理解。
  4. **跨线 vs 连接点语义**：每条合法跨线恰好在**次要线**上打一个桥（优先级：声明流向 → 折点少 → 更短），双桥收敛为单桥；落在 connection 上的跨线报 `DRAFT_CROSSING_ON_JUNCTION`；悬空连接点报 `DRAFT_JUNCTION_DANGLING`；**引擎永不新增/删除/重绑拓扑**（patch 永不含 `source`/`target`，出现即 `DRAFT_TOPOLOGY_CHANGED` blocker）。
  5. **保留空间（图例/标题栏/禁布区）**：走线把它们当障碍物参与评分、标签优先避开，并且**把未锁定却已经压在图例上的符号/连接节点确定性地移出**（本轮补的 `reserved_eviction` 阶段；锁定的侵入者留在原处并如实成为 blocker）。
  6. **质量门禁**：`passed` = 无 drafting blocker + 无图面规则 error + 分数 ≥ `target_score`（默认 95）；豁免（waiver）只是不阻断，finding 仍然可见。CLI 复用同一判定，门禁失败退出码 2，可直接当 CI 图面门禁。
  7. **可复现性**：id 规范序快照 + 规范序操作 + `input/output_content_hash` + `transaction_digest`（64 hex，摘要含引擎版本、请求、内容哈希与操作）；同样内容永远同一摘要，把结果再喂回引擎即 `settled`（幂等）。
  8. **接口面**：REST `POST /documents/{id}/drafting/report|preview`；MCP `get_drafting_report` / `preview_deterministic_drafting` / `apply_deterministic_drafting`；CLI `pid-agent drafting report|preview`（`--region/--element/--lock/--no-relayout/--no-reroute/--no-annotations/--no-bridges/--no-collisions/--target-score/--waive/--summary/--output`）；前端右侧新增只读「整理」tab（分析/预览/落地/丢弃/锁定/解锁 + 门禁解释 + 指标 diff + findings + 摘要）。
  9. **老图纸的降级契约（真实数据发现）**：把界面指向本地活库后，24 张非空图纸里有 2 张让 `/drafting/preview` 返回 500——它们引用了当前图例库已不存在的符号（`pressure_transmitter`），而 `DocumentService` 正当地拒绝计算这类元素的端口坐标。。现在：`unresolvable_element_ids`（未知图例符号 + 绑定到它的管线）从所有 pass 候选集中排除；报告给 `DRAFT_SYMBOL_DEFINITION_MISSING` 警告（不阻断门禁）；**锁定取证保持为空**（不让图例库缺失冒充“工程师锁定”）；两道安全网（阶段级 `InvalidOperationError` → `DRAFT_STAGE_UNAVAILABLE` 并停在最后一次成功提交；`service._symbol_port_point` 的 `KeyError` → `InvalidOperationError`）。修复后 **24/24 返回 200**。
  10. **真实数据实测（只读）**：24 张非空图纸（19→448 图元）逐个 preview：17 张产出事务、7 张已收敛（0 操作）、11 张分数提升、**0 回归**、0 写入；耗时中位数 ≈ 4.2 s，最慢 48 s（448 图元）。
  11. **顺手消掉一处真实重复实现**：端口绝对坐标原本在 `DocumentService` 与整理几何里各写一份；现在 `_symbol_port_point` 委托给 `drafting_geometry.symbol_port_point`，让“模块 docstring 声称的唯一实现”变成事实（Charter §6.4：点差一像素，导出才发现）。
- **M3 验收后窄补丁（外部验收唯一 blocker，已修）**：上一版 `commit()` 把每个阶段都与**整轮开始前的输入**比较，因此后面的阶段可以“把前面刚赢得的改善吐回去”而仍被接受（例：A 把某硬指标 5→1，B 把 1→4，旧实现因 4 ≤ 5 放行）。这与 M3 自己承诺的 monotone 契约不符。现在参照点是**最近一次被接受的状态**（`current` 只在阶段被接受时前进，并直接复用该阶段的候选快照，不额外重算当前图，成本与旧版相同）。`DRAFTING_ENGINE_VERSION` 1→2（接受语义变了，摘要必须能区分）。两道回归测试先证伪旧实现：5→1→4 时 B 必须回滚且 `DRAFT_STAGE_ROLLED_BACK.details` 含 stage 与 regression；逐次检查“比较参照点只能是最近接受态，不能回到输入，也不能是已回滚的状态”。**真实数据 A/B（同一份本地活库快照，v1 worktree vs v2，24 张非空图纸各跑两遍）**：两版都 24/24 deterministic、0 最终回归；448 图元图纸 v2 提交 **81** 个操作而 v1 是 **179**——差额正是旧实现放行过的“相对上一接受态恶化”的阶段；耗时 45.7 s → 40.4 s（无性能退化）；v2 在真实图纸上仍拿到全部**不冲突**的改善（unbridged_crossings 7→0、text_connector_intersections 27→0、text_symbol_overlaps 29→0、duplicate_label_count 25→17）。代价是诚实的：硬指标之间不再允许“此消彼长”，因此冲突图纸上 v2 的改动比 v1 更保守（该图 `pipe_obstacle_intersections` 不再下降）。这是契约要求的语义，不是退化。
- **M3 已知债务（不要藏）**：① **大图性能**：`commit()` 每次接受阶段都要重算整图图面质量，流水线最多 3 轮 × 多 pass，448 图元 ≈ 48 s；应急可用 `policy.pipeline_rounds=1` / `relayout=false`，正确修法是硬性指标的增量计算（后续任务）；② 老图纸的“不可用元素”与声明式稳定标识缺失只是如实上报，未被“修好”；③ 视觉基线仍在 macOS 生成，Browser job 因此持续红（既有问题）。
- **M3 验证（本地，2026-09-19，含单调性补丁）**：Ruff ✓；pytest **437 passed**（M2 基线 382；M3 首版 +50，本轮补丁再 +2 条阶段局部单调回归）；offline quality harness **6/6**（新增 `deterministic_drafting_contract`）；前端 `npm test` **117 passed**（基线 106，+11）；`npm run build` ✓；Playwright 本地全量 **45 passed / 3 failed**，3 个失败与 M2 基线**逐条相同**（`flow-runtime` 重命名定位 + `locked element badges` / `connector route anchors` 两个快照）。
- **视觉失败不是本轮引入（已用 A/B 实测，不是推断）**：在 `.freebuff/m2base` 建 M2 accepted baseline (`49e3e14`) 的 git worktree（node_modules 用软链接复用），同一台机器同一次运行两个存疑快照：基线 `22941` / `22574` 差异像素，本轮 `22868` / `22501`（ratio 都是 0.02，阈值 0.015），**同一批失败、同一量级**；且这些基线的最后一次更新在 `61cc572`（2026-08-21），早于 T0.5/M2。结论：既有基线漂移，非本轮 regression。
- **提交与推送状态**：M3 分四层提交并推送——`0cd15d5` 后端（引擎/几何/模型/接口/测试）→ `1b7d7dd` 前端 → `400ca52` 文档层 → `eb7f263` 真实数据鲁棒性修复（不可用元素降级 + 3 个回归测试 + 文档）→ `7054aff` 文档（记录 CI）→ **`b47f197` 验收后单调性窄补丁**（阶段局部比较 + 2 个回归 + 引擎版本 1→2 + 文档/真实数据 A/B）；`origin/main...HEAD = 0 0`，工作树除未纳入版本管理的 `.freebuff/` 外干净。未重切历史、未 force-push。
- **CI 真实结果（最新 commit `b47f197`，run `35367702779`）**：Backend Python 3.11 ✅（**pytest 437 passed**，与本地一致；quality harness **6/6**）；Frontend Node 24 ✅；Browser ❌ **39 passed / 9 failed**——失败集与 M3 基线 run `35364113950` **逐条相同**（1 个存量重命名定位 `flow-runtime` + 8 张视觉快照）。历史口径：最终 M3 首版代码 commit `eb7f263` 的 run `35364113950` 为 Backend ✅（pytest 435 passed、harness 6/6）/ Frontend ✅ / Browser ❌ 39✓9✗。关键点：9 个失败与基线 `c60f5be` **逐条相同**，**本轮新增的 3 个 drafting e2e 全部通过**（36 → 39 passed）。shared-mode security acceptance 仍因 browser job 红而 skip（已知债务）。
- **M3 明确边界**：不改标签文字/位号（重复位号只报不修）；不新增/删除设备、管线、连接点；不校验 ISA/SIS/cause-and-effect（属 M4 / Priority 2）；不产生声明式稳定标识（`identity_basis="element"` 的老图纸仍只有图元级标识，创建/导入层写出 `equipment_id`/`line_id` 的职责仍未开始）。文档见 `docs/deterministic-drafting.md`。
- **下一步（等 M3 外部验收后才可以开始）**：按依赖关系建议先做 **Priority 2 Validator Framework**，或按 milestone 顺序进入 **M4 Engineering Validation System**（configurable rules、stable issue codes、release validator、project profile）。两条路线都不许在未验收前动工。

## M2 及更早的状态（保留，最新在上）

- **提交与推送状态**：T0.5 证据链、Registry 收口、M2 首版已分三层提交并推送（`da2acf1` 后端 → `e34b6c2` 前端 → `5f96754` 文档）；**M2 返工同样已提交并推送**（`d13a480` 后端身份/Signal/OPC → `0997241` 前端 → `e221291` 文档），其后又追加一个窄修复 `fix: keep undeclared off-page connections unresolved unless service matches`（OPC 解析收紧，SHA 见本轮报告）。不存在“工作树待提交”的状态——若本行与 `git log` 不一致，以 `git log` 为准。
- **M2 返工（本轮，回应第二次验收的 3 个 Charter 级阻塞项）**：
  1. **工程身份与 tag 解耦**：`engineering_id` 改为不可变代理 id（`eq_…/vl_…/inst_…/sg_…/ln_…/jn_…/opc_conn_…`），来源由 `identity_basis`（`declared`/`element`）如实标注；tag 降级为可编辑属性（`tag`/`tag_key`），**tag 改名不再改变任何 engineering_id、边、trace 与索引行**；声明 id 冲突报 `IR_IDENTITY_COLLISION` 而不合并。
  2. **Signal 升格为一等工程对象**：新增 `signal` ObjectKind + `SignalDetail`（`signal_id` / `signal_type` / source / target / medium / 关联仪表与设备 / `classification` / `provenance`）；信号边与工艺边由 `edge_class` 分开，连通分量只跑工艺边，signal 不再只是 connector 的分类。
  3. **OPC 稳定连接身份**：每个 OPC 暴露 `off_page_connection_id`，项目索引解析对端后派生**对称且与 tag 无关**的 `connection_id`（由两端稳定对象 id 推导）；tag/service 只作交叉校核（`tag_agrees`），同 tag 反向的约定式匹配显式标注为 `reciprocal_declaration+service`。
- **M2 返工配套**：schema **v6**（`project_index.signal_count`，默认 0 = 真的是 0，不是未知）；`IR_BUILDER_VERSION` 升到 2，旧索引行**自动判 stale 并重建**，不会被静默混入新身份图；新增 REST `GET /api/v2/project/engineering-objects`、MCP `find_engineering_object`、CLI `pid-agent engineering-find`，trace 改为 `ref=`（稳定 id / tag key / tag / element id 均可，回报 `resolved_from`）；前端改为展示稳定身份与 identity_basis、Signal 计数与信号边、跨图稳定连接 id。
- **跨图 OPC 解析收紧（第二次验收的最后一项）**：`service_convention` 不再接受“目标图里仅有一个反向 OPC”作为证据——它现在要求 **normalised service 相同且方向相反且候选唯一**；不满足就 `resolved=False` + `matched_by="unresolved"` + `IR_CROSS_DOC_UNRESOLVED`（宁可承认未解析，也不猜出一条跨图连接）。同时 `declared_by_document_ids` 只在真正的相互声明时记两张图，约定匹配只记真正声明了目标图的那一侧；reciprocal 声明即使 service 不一致也成立（报 `IR_CROSS_DOC_TAG_MISMATCH`）。文档里的匹配矩阵与代码一致。
- **M2 返工验证（本地，2026-09-18）**：Ruff ✓；pytest **380 passed**（基线 359，+21）；offline quality harness ✓（含新 case）；前端 `npm test` **106 passed**（基线 105）；`npm run build` ✓；Playwright 本地全量 **42 passed / 3 failed**，3 个失败均为**基线即失败**的存量项（`flow-runtime` 重命名定位 1 项 + `locked element badges` / `connector route anchors` 两个快照），与 CI 在 `c60f5be` 的失败集合一致。
- **CI 真实结果（不是声明）**：推送后的 run `35314900492` = Backend Python 3.11 ✅、Frontend Node 24 ✅、Browser ❌（34 passed / 10 failed：1 个存量重命名定位 + 9 个视觉快照，其中 8 个在基线 run `35298219392` 就已失败）。相对基线**新增**的只有 `blank editor dark theme`，根因已定位为右侧面板 tab 条硬编码 5 列、第 6 个 tab 换行导致整条面板内容下移 38px（见下），本轮已修并用 e2e 守卫固化。
- **视觉基线的诚实结论（数字已于 M3 用实测修正）**：`frontend/e2e/visual.spec.ts-snapshots/` 最后一次更新在 **`61cc572`（2026-08-21）**（M2 时这里曾写作 `86710c0`，M3 用 `git log -- <snapshot dir>` 核对后修正），早于 T0.5/M2，**由 macOS 渲染器生成**，而 CI 在 Linux 渲染（字体度量不同）。因此 10 张快照里 8 张在基线 commit 上就长期红，属于**既有基线漂移**，不是本轮引入。本轮**不**从 macOS 重生成基线（那会把 macOS 字体烧进基线、复现同一个问题），建议另开一个“在 CI 渲染器中重生成视觉基线”的独立任务。

- **T0.5 Audit / Provenance 已完成**：SQLite schema v4 新增 append-only `audit_records`（sha256 哈希链，genesis → prev_hash/record_hash/ordinal），`audit.py` 负责把 revision 写入、semantic diff、validation evidence、approval/intent 绑定在同一条证据里；**所有写路径**（v2 文档/事务/undo/redo/重命名/移动、imports、project settings、MCP 全部 apply、legacy v1 primitive/layer、Agent harness allow/ask/deny、被拒绝的审批）都在**同一 SQLite 事务**内写入审计记录，不存在未归因的 revision。
- **T0.5 新增只读证据面**：REST `GET /api/v2/audit/records|verify|export`、`GET /api/v2/documents/{id}/audit`、`GET /api/v2/documents/{id}/history/{revision}/evidence`；MCP `get_audit_trail` / `verify_audit_chain` / `get_revision_evidence` / `get_agent_session_audit`；CLI `pid-agent audit verify|trail|export|evidence`。文档见 `docs/audit-and-provenance.md`。
- **Registry 收口（T0.3 补全）**：新增 `surface_contract.py`，把每个 HTTP 路由与 MCP tool 声明为 read / engineering_write / legacy_write / harness_lifecycle / runtime | agent_runtime / model_acceptance / project_metadata，并标记是否必须审计；`tests/test_surface_contract.py` 用**实时 OpenAPI + 实时 MCP tool 列表**反向校验。它建立的是**对当前暴露的 REST/MCP mutating surface 的机器检查完整性契约**（强回归守卫）：新增未登记写路径、未审计的工程写、未绑定的 side-effect tool 都会直接测试失败；但它不是“Python 内部绝不可能绕过 Store”的形式化证明。
- **M2 工程语义图（首版）**：新增 `engineering_ir.py`（工程对象、管线聚合、信号分类、OPC 跨图、拓扑边与连通分量、`IR_*` findings、flow-aware trace）与 `project_index.py`（`project_index` 派生索引：revision + content_hash + graph_hash + builder_version，cheap/verified 两级新鲜度、显式失效原因、自动修复、跨图 OPC 解析）。REST/MCP/CLI 三面齐备，离线 quality harness 新增 `engineering_graph_contract`。文档见 `docs/engineering-semantic-graph.md`。**首版的身份模型（tag 即主键）与 Signal（仅为 connector 分类）已在本轮返工中重做，见顶部状态。**
- **M2 前端**：右侧新增只读「工程图谱」tab（`frontend/src/editor/EngineeringGraphPanel.tsx` + 纯函数层 `frontend/src/engineeringGraph.ts`），展示工程对象分组、findings 严重度筛选、对象/管线定位、flow-aware trace 步骤与遍历管线、项目索引新鲜度/总计/跨图链接与显式重建按钮；新增 `frontend/tests/engineeringGraph.test.ts`（7 例）与 `frontend/e2e/engineering-graph.spec.ts`。
- **M2 明确边界**：IR 是 `(document, symbol registry)` 的纯函数，永不写文档；索引是派生缓存、不进审计链（但显式 rebuild 会记 `engineering.index.rebuilt`）；Signal 是对象但**尚未校验其工程正确性**（无回路线号/ISA 位号结构/SIS 联锁/cause-and-effect）；未声明稳定 id 的图纸仍只有 element 级标识（`identity_basis` 如实标注）；trace 目前是文本步骤列表，尚无拓扑图可视化；Agent 上下文仍走 `build_agent_harness_context`，与本图层尚未统一（属后续优先级，不是 M2 范围）。
- 已完成 Charter Priority 0 / T0.1–T0.4 首轮：Canonical Tool Registry、Agent Session、Permission/Approval Gate、Semantic Engineering Diff。
- Tool Registry 已统一 schema/permission/risk/side-effect/preview/idempotency/audit/surface；Agent/MCP/REST 共用能力定义。
- SQLite schema v3 已持久化 `agent_sessions`、`agent_approvals`、`agent_tool_calls`；approval 严格绑定 session + tool + document + canonical intent hash，成功执行后 consumed，旧 MCP/REST Agent mutation 旁路已封堵。
- semantic `plan-v2/stream` 创建 session，replan 续用，apply-v2 需要 session_id + approval_id；网页手动和自动 Agent 都在工程写入前停在人工确认点。
- T0.4 新增 `semantic_diff.py` / `semantic_diff_models.py`：将 raw before/after snapshot 转成 equipment/valve/instrument/pipeline 等工程对象级差异，输出 field delta、change type、human-readable summary 与 risk_hint。
- REST 新增无写入 `POST /documents/{id}/transactions/semantic-diff` 与按 revision 查询 `GET /documents/{id}/history/{revision}/semantic-diff`；MCP 新增 `preview_semantic_diff`。新 revision 的 history details 会持久保存 Semantic Diff。
- Semantic Diff 的 `risk_hint` 仅为 deterministic review hint，不替代未来 Rule Engine / safety rules 的正式工程风险判断。
- 首版核心验证（T0.5 + M2 首版，历史数字，已被本轮覆盖）：pytest 359、前端 105、schema v5；真实项目库（35 图）`rebuild_all` 35/35、0 stale、`audit verify` ok。本轮返工后：pytest **380**、前端 **106**、schema **v6**，详见顶部状态。
- **Playwright e2e 为什么之前没跑、现在怎么跑的**：`frontend/e2e/fixtures.ts` 的 `resetDocuments()` 会删掉所连数据库里的全部图纸，所以**绝不能指向本机活库**；同时 `playwright.config.ts` 的端口已改为可覆盖（`PID_AGENT_E2E_API_PORT` / `PID_AGENT_E2E_PREVIEW_PORT`，`vite.config.ts` 的 proxy 读 `PID_AGENT_API_TARGET`），因此可以在空闲端口上对着**临时数据库**跑。本轮即用 `PID_AGENT_E2E_API_PORT=8011 PID_AGENT_E2E_PREVIEW_PORT=4174 npx playwright test` 在隔离 DB 上完成全量执行，结果记在顶部状态。CI 侧不需要额外配置。
- **当時の“下一步”（已被 M3 取代，仅作历史）**：**Charter Priority 2 —— Validator Framework**：把当前分散在 `diagram_quality.py` / `engineering_reports.py` / `engineering_ir.py` 的 deterministic 规则收拢为可注册、可版本化、可按项目标准加载的 validator 框架（每条规则带 id/severity/scope/evidence/项目标准引用），并让 IR findings 成为其消费者。**注意编号**：Charter 的长期 milestone 是 `M2 Engineering Semantic Graph → M3 Deterministic Drafting Engine → M4 Engineering Validation System`；Validator Framework 属于 **Priority 2 implementation priority**，**不要把它改口叫 M3**（本轮之前 HANDOFF 里写成“下一步 M3 Validator Framework / 随后 M4 Drafting”，与 Charter 不一致，特此修正）。

## 近期轮次（最新在上，保留全部）

- 2026-09-22（R8：真实图纸 `气路系统总图.dwg` 本地复现 + TypeSafe 语义索引 + Blender 3D 模型，演示/验收轮）：
  - **做了什么**：拉起服务（重启后端到最新代码，PID 13802，托管已构建前端）→ 用项目自己的 CAD 导入管线把
    939 KB / AC1032 / 9757 图元的真实图纸导入本地 pidagent → 计时 → 用 TypeSafe System One 补语义索引。
  - **计时**：全幅复现 **8.37 s** 端到端 / 4.00 s 服务端（`doc_d17f1c7d174f`，9757 元素）；
    图面区域版 7.93 s / 3.90 s（`doc_72dabc6268d8`，9494 元素）；只读预演 4.21 s；TypeSafe 3.11 s。
  - **关键结论**：① `autocad-core-console` 路线 0 块定义缺失，比仓库既有 LibreDWG 文档多 515 图元；
    ② 图纸 extents 被游离内容撑到画布 58273.8×4772.2，图面本体只占最右 5437×3347，故保留全幅 + 裁切两份；
    ③ **`frame` 的 y 轴翻转**（原点是 `(x0, y1)`，`y0` 是下边界），按直觉填会溢出画布——已双写踩坑日志；
    ④ TypeSafe 对 112 标注 × 2 维 + 12 图层给出带概率的类型判定，41/112 可直接采用、49/112 进人工复核，
    产物为**旁挂候选**，未写进文档（P0「不发明工程语义」未越界）。
  - **产物**：`reports/pid-repro/`（`复现报告.html`、`final_drawing.png`、`main_drawing.png`、
    `semantics_candidates.json`、`ts_semantics.py`）。**未产生 git 提交**，工作树干净。
  - **R8 追加（Blender 3D 模型）**：TypeSafe 对 71 个设备标注定形体（15 类）+ 尺寸档（4 级）（3 请求 / 1.83 s）→
    `make_build_plan.py` 展开位号得 **116 个实例** → 经 blender-mcp 的 `127.0.0.1:9876` 裸 TCP socket 驱动
    Blender 5.2，在**新建独立场景 `气路系统3D`** 建出 116 台设备（14 个 `bmesh` 参数化形体函数），
    按图纸真实坐标 × `0.04 m/单位` 落位（厂区 197.7×114.9 m）。**隔离验证：用户场景 `Scene` 1019 对象零改动、
    `bpy.data.filepath` 未变、`window.scene` 已切回 `Scene`。**
    产物：`气路系统_3D.blend`（1.4 MB）、`3D模型报告.html`、三张渲染图、`blender_build.py` / `blender_drive.py` /
    `ts_blender_forms.py` / `make_build_plan.py`。能力边界如实声明：**设备本体 + 真实坐标布局，不含管线/连接关系**（守 P0）。
    踩坑 5 条已双写日志。**待用户操作**：`~/.workbuddy-ai/mcp.json` 的 `blender` server 需在连接器页点「信任」
    （本轮建模走直连 socket，未依赖该信任链）。
  - **R8 追加 2（覆盖气系统流程描述）**：应要求产出 `reports/pid-repro/覆盖气系统流程描述.md`——把整张总图读成
    **三条主线**（① 供气 5.0→1.0MPa；② 覆盖气主回路；③ 尾气与氚处理），并分「甲·设备清单与图面布局（高可信）」
    与「乙·流向叙述（结构性阅读，非机器验证）」两层交付，图上 6 条设计待办原文照录。
    分析方法可复用：**逐图层统计几何范围 + 长线分桶 + 文字按 x 排序 + 用图上明写的方向性标注交叉验证**。
    关键防误读：图上氩/氦两套气源是**并列候选方案（未定选）**，不是冗余备份；气路与主回路边界图上未定论。
  - **下一步**：回到 R7 的 A5 汇报 → 签 Gate → push；随后 **B（F6 首答/注入契约）**。

- 2026-09-18（M3 Deterministic Drafting Engine，本轮，已提交并推送）：
  - **新增文件**：`drafting_models.py`（契约/策略/门禁/可复现性模型）、`drafting_geometry.py`（纯几何与整理语义）、`drafting_engine.py`（七阶段确定性流水线）、`api_drafting.py`（两个只读 REST 路由）；测试 `test_drafting_geometry.py` / `test_drafting_engine.py` / `test_drafting_api.py` / `test_drafting_cli.py`；前端 `drafting.ts` / `draftingTypes.ts` / `editor/DraftingPanel.tsx` / `tests/drafting.test.ts` / `e2e/drafting.spec.ts`。
  - **落地要点**：锁定三来源统一（含 `layout_regions` 的 `kind="lock"`）；区域/选择范围修复（越界即 blocker）；forced vs opportunistic 重路由（“不要顺路整理”≠“把你刚挪走的管子留在原地”）；跨线单桥只打次要线；保留空间既参与评分又驱逐未锁定侵入者；最后把结果再喂回引擎验证 `settled`。
  - **提交**：`0cd15d5` 后端 → `1b7d7dd` 前端 → 文档层，已推送。
  - **真实数据发现并修复**：界面指向活库后 2/24 张图纸让 preview 500（引用了已不存在的图例 key）。修复：不可用元素集合 + 阶段级降级 + `KeyError` → `InvalidOperationError`；新增 3 个回归测试（几何/引擎/API 各一）。
  - **验证**：Ruff ✓；pytest **435**（M2 基线 382，+53）；quality harness **6/6**（新 case `deterministic_drafting_contract`）；前端 **117**（基线 106）；`npm run build` ✓；Playwright 本地 **45 passed / 3 failed**，失败集与 M2 基线逐条相同（已用 worktree A/B 实测证明，见顶部状态）。CI（run `35359869866`，commit `1b7d7dd`）：Backend ✅ / Frontend ✅ / Browser ❌ **39 passed / 9 failed**，失败集与基线逐条相同，新增 3 个 drafting e2e 全通过；后续补丁另行推送（SHA 见本轮报告）。
  - **文档**：`docs/deterministic-drafting.md`（新增）、`docs/offline-quality-harness.md`（四个检查 → 六个检查，补齐 M2/M3 两个契约）、`docs/tool-registry.md`（登记 30 个实时能力与 M3 三个工具）、README、`REUSE_AND_PITFALL_LOG.md`。
  - **顺手修正**：`DocumentService._symbol_port_point` 改为委托 `drafting_geometry.symbol_port_point`，消除端口坐标的第二份实现（模块 docstring 原先声称“唯一实现”，但代码并不是——本轮把它变成事实）。

- 2026-09-18（M2 返工：稳定工程身份 + 一等 Signal + OPC 稳定连接身份，已推送 T0.5/Registry/M2 首版）：
  - **验收意见落地**：① `engineering_id` 与 tag 解耦（`eq_…` 等不可变代理 id，`identity_basis` 如实标注 declared/element，冲突报 `IR_IDENTITY_COLLISION`）；② `signal` 升为一等对象（`SignalDetail` + `signal_id`，`edge_class` 分离信号边/工艺边，连通分量只跑工艺边）；③ OPC 新增 `off_page_connection_id` 与索引层**对称 tag-free** 的稳定 `connection_id`，tag 仅作交叉校核。
  - **schema/迁移/兼容**：schema v6（`project_index.signal_count`）；`IR_BUILDER_VERSION=2` 使旧索引行自动 stale 并重建；v5→v6 迁移测试；缺新 id 的老图纸不会坏（降级为 element 身份并如实上报），旧 builder 行在 `find_objects` 中被跳过而不猜测。
  - **新增接口**：REST `GET /project/engineering-objects`、MCP `find_engineering_object`、CLI `pid-agent engineering-find`；trace 改为 `ref=`（id/tag key/tag/element）并回报 `resolved_from`。
  - **前端**：面板展示稳定身份 + identity_basis、Signal 计数/信号边、跨图稳定连接 id；修掉一个真实布局回归——右侧 tab 条原来硬编码 5 列，新增第 6 个 tab 会换行（条高 39→77px）并把面板内容下移 38px，改为与个数无关的 `grid-auto-flow: column` 并新增 e2e 守卫（tab 必须同一行且在 dock 内）。
  - **验证**：Ruff ✓；pytest **380**；quality harness ✓；前端 **106**；`npm run build` ✓；Playwright（隔离 DB、可覆盖端口）**42 passed / 3 failed**，3 项均为基线即失败的存量项（与 CI `c60f5be` 失败集合一致）。CI（推送后 run `35314900492`）：Backend ✓ / Frontend ✓ / Browser ❌（34✓10✗，其中 8 张快照在基线就红，新增的 dark 快照已定位并修复）。
  - **第二次验收后的窄修复**：跨图 OPC 解析不再把“目标图里唯一的反向 OPC”当成匹配证据——`service_convention` 要求 normalised service 相同 + 方向相反 + 候选唯一，否则 `unresolved` + `IR_CROSS_DOC_UNRESOLVED`；`declared_by_document_ids` 不再为约定匹配记账目标图。新增 2 个回归测试（一个反向 OPC 但 service 不同 → 保持未解析；同 service 约定匹配 → 只记声明侧，含空白/大小写容错与 `PL2002`≠`PL-2002` 的反例）。

- 2026-09-18（T0.5 Audit/Provenance + Registry 收口 + M2 工程语义图首版，已提交并推送 `c60f5be..5f96754`）：
  - **证据链**：schema v4 `audit_records`（`record_id/ordinal/recorded_at/event_type/actor/surface/tool_name/status/document_id/base_revision/result_revision/session_id/approval_id/tool_call_id/provider/model/label/intent_hash/diff_hash/diff_preview_hash/diff_binding/validation_status/validation_hash/evidence_json/error_code/prev_hash/record_hash`）；`audit_hash.py` 单点计算链哈希；`store.save()` 与 `store.record_audit_event()` 把文档行、history（含 semantic diff）、审计行、tool call、approval 消费、session 关单**放在同一个 `BEGIN IMMEDIATE` 事务**；新增 `request_context.py` 提供 request-id 关联（中间件绑定，API 只从服务端上下文取 actor/surface，绝不信任请求体）。
  - **写路径全覆盖**：v2 transactions/undo/redo/rename/folder、imports（document/project-package）、project settings、create/delete、MCP `apply_transaction`/`apply_transaction_v2`/`apply_agent_transaction`/`apply_auto_layout`、legacy v1 draw/layer 与 undo/redo、以及 **被拒绝/意图不匹配的审批**（`permission.rejected`、`validation.rejected`）都落审计；MCP 旧的 bespoke apply 路径已改为复用唯一受治理写通道（`_apply_with_history` → 同一 harness gate）。
  - **Registry 收口**：`surface_contract.py` + `tests/test_surface_contract.py` 建立“无未登记写路径”机器契约；`tool_registry.py` 为每个 side-effect tool 补 `audit_event` / `idempotency`，并新增 engineering graph / trace / project graph / index rebuild 四个能力定义（rebuild 为 `allow` + `idempotent` + 审计 `engineering.index.rebuilt`）。
  - **M2 工程语义图**：`engineering_ir.py`（稳定工程标识：tag 级优先、重复 tag 确定性 `#2` 且报错、未打标签如实降级为 element 级；管线按 tag+介质+口径聚合；信号仅按显式介质或 instrument↔instrument 分类，绝不把工艺引压点当信号；OPC 方向/目标图；拓扑边 + 连通分量 + 7 类 `IR_*` findings + flow-aware `trace_engineering_object`）；`project_index.py`（schema v5 `project_index` 缓存 + `document_content_hash` 失效检测 + `rebuild_all/prune` + 跨图 OPC 解析 + `ProjectEngineeringGraph`）。
  - **接口**：REST `GET /api/v2/documents/{id}/engineering-graph`、`.../engineering-graph/trace`、`GET /api/v2/project/engineering-graph`、`GET /api/v2/documents/{id}/project-index`、`POST /api/v2/project/index/rebuild`；MCP `get_engineering_graph` / `trace_engineering_object` / `get_project_engineering_graph` / `rebuild_project_index`；CLI `pid-agent engineering-graph`、`pid-agent project-index rebuild|list|project`（有 error finding 或 stale 时退出码 2，可直接进 CI）。
  - **测试**：新增 `tests/test_audit_provenance.py`、`tests/test_surface_contract.py`、`tests/test_engineering_ir.py`（20）、`tests/test_project_index.py`（11）、`tests/test_engineering_graph_api.py`（5），质量门新增 `engineering_graph_contract`，迁移测试覆盖 v3→v5 与 v4→v5；并修复一处**测试自身的隐性缺陷**（v3 迁移测试原先硬编码 `CURRENT_SCHEMA_VERSION - 1`，版本号一涨就失真，现改为显式 `PRAGMA user_version=3`）。
  - **真实数据交叉验证**：对仓库 30 张真实图纸 `rebuild_all`：1440 工程对象、536 拓扑边、28 个 OPC、264 ms、0 stale；同时暴露真实数据问题 18 处 `IR_SYMBOL_DEFINITION_MISSING`（如 `pressure_transmitter` 并非图例库 key）、27 处 OPC 缺 `target_document_id`、55 处孤立设备 —— 均为**既有图纸数据缺陷**，非本轮引入，作为 M3/M4 的输入保留。
  - CI 口径：Ruff✓、quality harness 5/5✓、pytest 359✓；前端本轮未改动。Playwright e2e 未复跑（8000 端口被常驻后端占用），沿用既有基线问题清单。
- 2026-09-18（T0.4 Semantic Engineering Diff）：新增工程语义 diff 模型/引擎，支持 valve/equipment/instrument/pipeline/junction/annotation 等实体分类、field delta、layout/reroute/engineering-property change 分类与 risk hint；提供 transaction 无写入 preview、revision 历史持久化和 REST/MCP 查询；新增 4 组 diff 测试。核心 CI：Ruff✓、quality harness 4/4✓、pytest 286✓、frontend unit/build✓。下一步 T0.5 Approval + Diff + Validation Evidence provenance。

- 2026-09-18（T0.2/T0.3 Agent Session + Permission/Approval Gate）：数据库升级 schema v3，新增 sessions/approvals/tool_calls；实现 exact-intent approval hash、allow/ask/deny enforcement、one-time consume、session audit；semantic plan/replan/apply、网页手动/自动 Agent、MCP semantic/low-level apply 全部接入 Harness，封堵旧低层绕过；新增 REST/MCP 管理接口、前端显式批准、集成测试与设计文档。下一步 T0.4 Semantic Diff。

- 2026-09-18（T0.1 Canonical Tool Registry）：新增统一 Tool Registry，登记 12 个现有 Harness 能力并固化 schema/permission/risk/side-effect/preview/idempotency/audit metadata；REST 新增 `/api/v2/agent/tools`，MCP 新增 `get_tool_registry`，semantic tool schema 改由 registry 派生；新增单测与设计文档。保持 DocumentService/Transaction 原子边界不变。下一步 T0.2 Session + T0.3 Permission/Approval enforcement。

- 2026-09-18（总体技术任务书与 AgentCAD Harness 长期主线）：新增 `PROJECT_CHARTER.md` v1.0.0（约 2.45 万字符），把最终“经工程校核批准后可进入施工阶段交付”的目标固化为 canonical charter；明确 semantic IR、受控 tools、validator、permission/approval、audit/provenance、project graph、release gates、benchmark 和 M0-M10；同步更新 AGENTS.md 强制未来模型先读 Charter，并在 README 建立入口。下一步优先 T0.1 Tool Registry → T0.2 Session → T0.3 Permission/Approval → T0.4 Semantic Diff → T0.5 Audit。本轮仅文档治理，无业务代码变更、未重跑测试。

- 2026-08-21（全项目代码 Review：质量声明核验 + 安全/性能深审）：实测核验 pytest 271✓ / npm test 98✓ / build✓；**ruff 实测 9 错**（security.py F821×8 缺 `Any` 导入 + F841×1），与「ruff 0 报错」声明不符；e2e 因 8000 端口被常驻后端占用未复跑。关键发现：🔴 security.py `_handle_asgi` 仅校验 Content-Length 头，chunked 请求可绕过 body 大小上限（F841 未使用变量即烂尾证据）；🔴 验收矩阵链路（api_acceptance→model_acceptance）未注入 ProviderNetworkPolicy，shared 模式下该端点可作 SSRF 跳板（llm.py 默认 local 策略放行全部地址）；🟡 长 SSE 流全程占用并发信号量槽（默认32）；🟡 前端 EditorCanvas pointermove 触发整画布重渲染、api.ts SSE `.trim()` 吞流式空白、main.tsx 挂载 4 个 MutationObserver 旁路补丁组件、store/SSE 解析零单测覆盖。下一步：① 补 `from typing import Any` 清零 ruff；② ASGI 路径落地真实 body 限制；③ create_acceptance_router 注入 provider_policy；④ 按 Top5 清单治理前端渲染与旁路组件。
- 2026-08-21（左侧侧边栏滚动冲突根除、全域滚轮穿透与导航标签体系）：移除了 .document-tree-list 内部嵌套限高，外层 .sidebar.documents-panel 统一滚动（scrollbar-gutter stable / 高对比度滑块）；左侧新增 全部/图纸/图例 三档导航与 一键折叠全部 快捷按钮；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（左侧图纸列表滚轮滚动条与左下角单位图例基础图元过滤）：优化 .document-tree-list 样式与专属滚动条（max-height 480px / 细滚动条 / overscroll-behavior contain），滚轮顺畅触达所有图纸；SymbolPalette 过滤基础图元，保持左下角单位图例纯净聚焦工艺设备；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（大模型流式传输阻塞根因修复与 Ollama / MiniMax M3 Cloud 真实联调验收）：排查并消除前置同步 post 与 Starlette BaseHTTPMiddleware TaskGroup 协程中断冲突，将 RequestDiagnostics 与 RequestBoundary 重构为原生纯 ASGI 中间件；使用本地 Ollama 与 MiniMax M3 Cloud 真实联调端到端 P&ID 出图流式测试，100% 成功生成储罐与离心泵标准对齐管线，质量评分 100.0 分；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（基础图元顶部分类快捷栏与大模型思考/生成双流式传输）：基础图元放置免属性弹窗直接落图；顶部工具栏新增 BasicShapesToolbar（几何/设备/标注/附件 4 分类，支持直接拖拽至画布）；后端实现 plan-v2-stream SSE 流式接口提取 reasoning_content 与 <think> 思考链；前端 AgentStreamingViewer 实时动态渲染思维链；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 42 passed 100% 全绿。
- 2026-08-21（项目分类文件夹树状归档与通用基础图元库移植）：左侧新增项目分类文件夹管理（新建/重命名/删除/折叠展开/下拉移动/搜索清空），支持新建图纸时直接指定分类；扩充 11 种通用基础图元（变更云线/六角框/八角安全框/菱形判定/圆柱体/机柜撬块/梯形槽/平行四边形/标注气泡/粗箭头/8字盲板/阻火器等）；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 41 passed 100% 全绿。
- 2026-08-21（第三步落地：属性面板三段式与图例悬停放大预览）：借鉴 draw.io 重构右侧属性面板为三段式分类（全部/工程/样式/排列），集成快捷对齐、等间距分布与编组锁；左侧图例库新增悬停放大 Popover（大号矢量预览、Badge、尺寸与端口引脚清单）与搜索一键清空；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（第一步与第二步落地：标准图元扩充与连线对齐增强）：在 standard_symbols.json 扩充 10 种工业高频图元（角阀/三通调节阀/减压阀/精馏塔/螺旋板换热器/螺杆泵/罗茨风机等）；实现智能磁吸等间距吸附（dist(A,B)==dist(B,C)）与放置吸附；全量回归：pytest 271 passed，npm test 98 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（物项专属工程属性弹窗与下拉自填体系）：实现物项放置时的分类专属工程属性弹窗（5 大类 Schema：阀门/设备/管线/仪表/管件）与右侧属性栏同步；预设丰富标准通径、压力、材质、故障位置等参数，原生支持下拉选择与自由输入自填，支持直接跳过；全量回归：pytest 271 passed，npm test 96 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（物项专属工程属性弹窗与下拉自填体系）：实现物项放置时的分类专属工程属性弹窗（5 大类 Schema：阀门/设备/管线/仪表/管件）与右侧属性栏同步；预设丰富标准通径、压力、材质、故障位置等参数，原生支持下拉选择与自由输入自填，支持直接跳过；全量回归：pytest 271 passed，npm test 96 passed，Playwright e2e 40 passed 100% 全绿。
- 2026-08-21（取消超时硬顶与随时手动叫停机制）：针对本地大模型生成耗时较长的特点，取消超时硬编码与 600s 校验上限（默认无超时持续等待）；移除前端超时数字输入限制；全链路接入 AbortController，手动模式与自动完成均新增即时「停止 / 叫停」按钮；全量回归：pytest 271 passed，ruff 0，npm test 92 passed，e2e 39 passed 100% 全绿。
- 2026-08-21（厂商解耦与零默认模型纯净化）：彻底移除源码与预设中任何厂商名称（如 Kimi）与默认模型名（`defaultModel` 全部置空）；`providerPresets.ts` 纯净化为通用标准选项；后端 `provider_compat.py` 改造为通用协议层；`README.md` 与文档同步纯净化；全量回归：pytest 270 passed，npm test 92 passed，Playwright e2e 39 passed 全绿。
- 2026-08-21（商业化纯净化与存量 e2e 全通）：按商业化交付标准彻底清理私有域名/测试报告/路径；`provider_compat.py`、`llm.py`、`client.py`、`api_acceptance.py` 常量全接入 `os.getenv` 动态机制；根目录新增 `.env.example`；修复 OPC 双击跳转与 effective timeout 两个存量 e2e，全量 39 个 e2e 首次全绿。

- 2026-08-20（硬编码审查）：全面审查后端和前端代码中的硬编码问题。工具链：grep 搜索 URL/端口/路径/密钥/模型名/超时等模式。发现并分类如下：

  **🔴 值得关注（3 项）**
  -① `backend/agentcad/provider_compat.py:9` —— `KIMI_CODING_BASE_URL = "https://api.kimi.com/coding/v1"` 硬编码为模块级常量，未通过环境变量暴露；Kimi 若迁移 API 地址需改代码。
  -② `backend/agentcad/provider_compat.py:10-17` —— `KIMI_CODING_MODEL_IDS`、`KIMI_K3_MODEL_IDS`、`KIMI_K3_MAX_COMPLETION_TOKENS`(8192)、`KIMI_K3_VISION_MAX_COMPLETION_TOKENS`(16384) 硬编码；模型新增/改名/调限额需改代码。
  -③ `backend/agentcad/api_acceptance.py:19` —— 内嵌 HTML 表单默认值：`base_url="https://apihub.agnes-ai.com/v1"`、`model="agnes-2.0-flash"`、`timeout=120`、`repetitions=3`、`replans=3`；指向特定第三方服务，下线需手动更新。

  **🟡 建议关注（4 项）**
  -④ `backend/agentcad/client.py:32-34` —— Python SDK 默认 `base_url="http://127.0.0.1:8000"`、`timeout=120`；非本地部署每次需传参覆盖，且超时与服务端默认 600s 不一致。
  -⑤ `backend/agentcad/llm.py:447-448` —— 错误消息硬编码 Kimi 模型名和 URL，未引用 `provider_compat.py` 常量，维护时易遗漏。
  -⑥ `frontend/src/providerPresets.ts` —— 7 个预设 Provider 的 Base URL 全部硬编码（OpenAI/Kimi/DeepSeek/OpenRouter/Groq/Ollama/LM Studio），URL 变更需重建前端。
  -⑦ `backend/agentcad/config.py:60,66,72` —— 默认数据库路径/默认 CORS/默认 frontend dist 虽有环境变量覆盖，但 Docker 镜像中已重新指定，三者可能不一致。

  **🟢 可接受（设计上合理的默认值）**：云元数据安全地址、符号文件路径、Dockerfile 端口/Docker-Compose 变量、e2e 测试固定值、build_argon_pid.py 一次性脚本、config.py 的 byte/timeout 默认值（均有统一 _env 机制）。

  **✅ 已正确处理**：前端 API_ROOT（VITE_API_ROOT 可配置）、LLM 连接参数（PID_AGENT_LLM_BASE_URL 等 env 变量）、config.py 全字段（_env + 主/备环境变量名）。

  下一步：① 将 KIMI_CODING_BASE_URL 等常量改为环境变量可覆盖；② 考虑将 providerPresets 通过 API 下发；③ 验收测试页面的默认值抽取到环境变量。

- 2026-08-20（路演 PPT）：pitch/ 生成 18 页青创大赛路演 PPT（依据 P072 项目计划书 docx，嵌入仓库真实插图：氩气 P&ID、阀门图例、e2e 截图；数据口径=模型矩阵 15/15、271 后端测试等）；deck.js 可重建，slides_test 溢出检测通过 + Vision OCR 逐页核验；成品同时复制至 P072/青创大赛/。坑：soffice 需加 PATH；describe_image 本宿主不可用→OCR 替代；已记踩坑日志。下一步：人工审 montage 定稿。

- 2026-08-19（UI 视觉重构）：分支 `ui-polish-2026-08-19` 提交 86710c0——styles.css token 化重写（浅/深双主题）+ 顶栏 ghost 按钮 + 浅色 document-bar + 细滚动条/焦点环统一，导出 PNG 绿边框弱化；10 张视觉快照重生成。门槛：npm test 92✓ / build ✓ / pytest 271✓ / e2e 37✓+2 存量失败（effective timeout 缺 `PID_AGENT_AGENT_TIMEOUT_SECONDS=180`；OPC 双击跳转，基线同样失败）。已合入 main 并推送（a0829dd）。下一步：另开任务修 2 个存量 e2e。

- 2026-08-17（Git 标准化）：工作区统一规范——.gitignore 补全 .reasonix/ 和 credentials*；无业务代码变更

- 2026-08-13：补齐 README 的 Purpose/Status/Stack/Commands/Structure/Configuration/Notes，新增需求与任务文档；确认本机数据库/WAL/SHM 均为 `0600`，未读写数据库或业务代码。
- 2026-08-13：依据 README 与产品边界补齐交接；未改业务代码或数据库。
