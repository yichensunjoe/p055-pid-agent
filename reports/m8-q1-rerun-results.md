# M8-Q1 冻结 corpus 重跑结果（Q1R + Q1R2 修复后，m8-q1r）

> 环境：worktree `P055-PID-Agent-q1r`。本文件对应最终证据提交 `4c85b5119`（HEAD 已核）；runner 在证据写入前捕获的 `git status --porcelain` = **空**（干净树），`agentcad.__file__` 指向 worktree，corpus lock：source commit `25219f8e`、blob `c4442482…`、expected 17 = actual 17 步。scratch DB 跑完已删；corpus 全程未改；HOLDOUT 只验收未反改。

## 逐场景（8 场景 17 步）

| 场景 | 结果 | 说明 |
| --- | --- | --- |
| DEV-1 | 句一 ✅complete r1；句二 ✅complete r2 | DXF 6.3KB 真实图元 |
| DEV-2 | 句一 422 receipt（missing_selector）；句二 ✅**complete** | ambiguity_resolution_rate = **1/1** |
| DEV-3 | 句一 ✅complete；句二 422 布局不可走线（cn_5，G4 遗留，**错误文本完整留存**） | DXF 8.2KB |
| DEV-4 | 句一 422 partial：三仪表独立入账，LIT/PIT 判定成功，TT-101 独立 structured catalog gap；句二 422 无 base（如实记录） | 空文档 |
| DEV-5 | 句一 422 partial：**系统声明 receipt「添加一个主工艺系统：系统声明暂不支持，已记录、未生成设备」，无 phantom E-01**；句二 422 无 base（如实记录） | 空文档 |
| DEV-6 | 句一 ✅complete；句二 422 selector_no_match:fractionation_column（回流入口，G3 遗留）；句三 422 依赖句二 | DXF 5.5KB |
| HOLDOUT-1 | 句一 ✅complete（5 设备 4 连接）；句二 422「换成」unknown 子句 receipt（符合 corpus 预期） | DXF 10.9KB |
| HOLDOUT-2 | 句一 ✅complete r1；句二 ✅complete r2（删除 P-102 后 4 设备 3 连接） | DXF 7.9KB |

## Gate 重跑验收对照（R2 版）

1. **17-step frozen corpus 完整重跑** ✓（runner 内置 corpus lock，步数漂移即断言失败）。
2. **DEV-5 绝无 phantom E-01，系统声明得到诚实 receipt** ✓（fail-closed receipt 在 raw 的 skipped 字段可见；单测断言 spec 无 E-01、completeness=partial）。
3. **所有 4xx 保存完整错误原因** ✓（DEV-3 布局拒绝全文、参数校验明细、结构化 receipt 均留存）。
4. **clean provenance** ✓（status 于证据写入前捕获，为空）。
5. 此前四项（DEV-2=1/1、DEV-4 独立层、实体 DXF readback、拒绝零副作用）保持 ✓。

## 修复清单（m8-q1r，基于 origin/main @ 1afeccab）

- `e1a7c20` P1 B' 预提交对账（service.py 新增 keyword-only precommit_validator）+ G7 route parity（_plan_rows 端点经唯一端口映射规范到精确锚点）。
- `068ebc2` G2 设备添加枚举分解（一台X、一台Y 和一台Z → 独立 clause）。
- `399ff78`（amend 后 tip 代码同）Q1R2：系统声明 fail-closed receipt（m7_text_planner，read() 经 `last_system_declarations` 属性传出，plan() 入 skipped 账本，设备路径永不接触）；runner 恢复 DEV-5 句二 + 17 步 corpus lock + 4xx 全因留存 + 证据前干净 provenance。
- 证据：`4c85b5119`（raw + provenance + 本文件）；旧 16 步证据已被替换。

## 遗留真实 gap（归类不变，Q2 输入）

G4 DEV-3 泄放支路走线（router 白名单外）；G3 族 DEV-6 回流/侧线 selector 词表；TT-101 温度变送器符号缺失；HOLDOUT-1「换成」语法。另：DEV-5 句二因句一 receipt 无 base 未执行——这是可信的 system-support gap 记录，corpus 预期本就如此（「当前是 unknown 子句 → receipt」）。
