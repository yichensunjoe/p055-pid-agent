# M8-Q1 冻结 corpus 重跑结果（Q1R 修复后，m8-q1r @ 4c9091a）

> 环境：worktree `P055-PID-Agent-q1r`，git HEAD `4c9091a202f0877819890bff4e897e96e4d26b9a`，status 仅本证据文件 untracked；server cwd = worktree，`agentcad.__file__` 指向 worktree（provenance 见 `m8-q1r-rerun-provenance.json`）。scratch DB `m8-q1-rerun.db` 跑完已删。corpus 全程未改（HOLDOUT 原句冻结）。

## 逐场景（8 场景 16 步）

| 场景 | 句一 | 句二 | 句三 | DXF 实体导出 |
| --- | --- | --- | --- | --- |
| DEV-1 | ✅ complete r1 | ✅ complete r2（3 设备 2 连接） | — | 200，6.3KB 真实图元 |
| DEV-2 | 422 receipt（missing_selector） | ✅ **complete**（管程入口→selected→提交） | — | 200，6.8KB |
| DEV-3 | ✅ complete r1（4 设备 3 连接） | 422 布局不可走线（cn_5，G4 遗留） | — | 200，8.2KB |
| DEV-4 | 422 partial：三个仪表独立入账，LIT/PIT 判定成功，**TT-101 独立 structured catalog gap**（温度变送器无符号） | 无 base 未执行 | — | （空文档）991B |
| DEV-5 | ✅ complete（系统声明+E-101 管程解析） | — | — | 200，7.3KB |
| DEV-6 | ✅ complete（原料进料口解析成功） | 422 selector_no_match:fractionation_column（回流入口，G3 遗留） | 422 依赖句二 | 200，5.5KB |
| HOLDOUT-1 | ✅ complete（5 设备 4 连接） | 422「换成」unknown 子句 receipt（符合 corpus 预期） | — | 200，10.9KB |
| HOLDOUT-2 | ✅ complete r1 | ✅ complete r2（删除 P-102 后 4 设备 3 连接） | — | 200，7.9KB |

## Gate 四项重跑验收对照

1. **DEV-2 ambiguity_resolution_rate = 1/1** ✓（receipt → 重述「管程入口」→ selected → complete）。
2. **DEV-4 走到独立仪表 candidate / structured gap 层** ✓（G2 生效：三个仪表各自独立；TT-101 得到自己的 catalogue-gap receipt，不再合并暴毙）。真实 gap：catalogue 无温度变送器符号 → Q2 输入。
3. **≥1 份实体 DXF 200 + readback PASS** ✓（7/8 份非空实体 DXF 全部 200 且 read_dxf 解析出真实图元种类；空的那份是 DEV-4 句一未提交所致）。
4. **rejected 请求前后状态一致** ✓（P1 B' 单测 T1：GET/store 状态全等；e2e「partial plan refused with receipt and changes nothing」6/6 通过）。

附加：同一 scratch process 8 场景 + 基线隔离守卫无污染；HOLDOUT 只验收未反改。

## 遗留真实 gap（全部已归类，非 Q1R 范围）

- G4：DEV-3 泄放支路正交走线失败（auto_layout/router，白名单外）。
- G3 族：DEV-6「回流入口」「侧线采出口」selector 词表（Q2 catalogue correction）。
- DEV-4 TT-101：温度变送器符号缺失（Q2 expansion 输入，当前 BLOCKED 待 M8-Q1 PASS）。
- HOLDOUT-1「换成」：替换语法 unknown 子句（receipt 正常，属覆盖扩展候选）。

## 修复与验证汇总（m8-q1r 分支，基于 origin/main @ 1afeccab）

- P1 B'：`service.apply_transaction` 新增 keyword-only `precommit_validator`，`_stage_mutation()` 后、store.save 前对同一个 working document 对账，失败抛 `PrecommitValidationError` 由 m7 译为 MaterializationDocumentError（409 零副作用）。
- G7：`_plan_rows` 构造 connector 行时经 `drafting_geometry.symbol_port_point`（唯一端口映射实现）把端点规范到精确端口锚点，内部走 writer 自己的 `_bind_manual_endpoints` 规则（幂等）。
- G2：`_expand_device_add_enumeration` 只拆「一台 X、一台 Y 和一台 Z」设备添加枚举（每项须自带位号、含连接/删除动词不拆、非量词开头不拆）。
- 测试：新增 `test_m8_q1r_route_parity.py`（G7 回归 + P1 原子性）、`test_m8_q1r_clause_decomposition.py`（G2 五例）；全量后端 1622 测试分片全绿；前端构建通过；e2e nl-draw 6/6（含画布提交/改图/拒绝零副作用）。
- runner：`reports/m8_q1_runner.py` 绝对路径改仓库相对（主库文档分支 `5dbcc00` + 本分支 `4c9091a`）。
