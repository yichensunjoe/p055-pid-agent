# DI-1-D3 基线证据（延迟 / replay / gap 断言）

Gate CODE GO 范围（2026-10-12）：仅基线测量 + 语料锁定 + gap 断言基线，零产品行为变更。本文件由 `reports/di1_baseline_runner.py` 产出，原始数据在 `reports/di1-latency-baseline.json`（含双 run 全部样本与 provenance）。

## 语料与锁定

- 语料：`backend/tests/di1_corpus/corpus.json`（S1–S6，Gate DI-1-D2 冻结），sha256 `5a2450d9946acf76…`；lock 测试 `backend/tests/test_di1_corpus.py`（4 项：digest 钉、结构、五类覆盖、gap 覆盖）。
- 实测环境：macOS-26.6.1-arm64-arm-64bit / Python 3.12.13 / 真实 TypeSafe 端点；每 run 独立 scratch DB + 独立 uvicorn 子进程；HEAD `4deee20da`。

## 延迟基线（判读式链路 text-plan/text-edit，真实 TypeSafe）

- 服务端规划延迟（`latency_ms`，8 个样本）：median **705.1 ms**，min 610.4，max 1515.6。
- 全语料端到端（6 场景含建文档与提交）：run1 7513.9 ms / run2 5552.1 ms。
- 口径：样本全列、不外推、不作 CI ceiling（M13 证据方法裁定）。

## deterministic replay（F2 第三指标的现状）

- 双 fresh-process run 逐场景比对：**6/6 场景 spec_digest 与 outcome 双一致**（digest_match 与 outcome_match 均为 True）。

## 语义正确性现状（semantic correctness）

| 场景 | 结果 | 说明 |
|---|---|---|
| S1 | ✅ 全对 | committed |
| S2 | ❌ 未完整交付（诚实拒绝） | 422:typesafe_spec_partial |
| S3 | ❌ 未完整交付（诚实拒绝） | 422:typesafe_spec_partial |
| S4 | ❌ 未完整交付（诚实拒绝） | 422:typesafe_spec_partial |
| S5 | ✅ 全对 | committed → 422:typesafe_spec_partial |
| S6 | ✅ 全对 | committed → committed → committed |

## gap 断言基线（冻结 gap 清单的当前真相）

| gap | 现状 | 证据 |
|---|---|---|
| G3 回流/侧线 selector 词表 | **已修通** | S6 三句全 committed（回流入口 + 侧线采出口均落地） |
| G4 泄放支路走线 | **仍拒（gap 在）** | 探针复现：这句话规划出的图纸引擎目前画不了：connection 'cn_3' cannot be routed orthogonally from 'el_PSV_10… |
| TT-101 温度变送器符号 | **已修通** | 探针 committed，产出 V-101=buffer_tank, TT-101=temperature_transmitter |
| HOLDOUT-1「换成」语法 | **仍拒（诚实 receipt）** | S5 改图句 422 typesafe_spec_partial：无法理解的子句：「把 XV-101 换成一个控制阀」 |
| SYSTEM-DECL 系统声明子句 | **按设计诚实拒绝**（fail-closed，非缺陷） | 探针 422:typesafe_spec_no_device |

## 基线新发现的 gap（D4 实现片的候选工作项）

| 场景 | 拒识/缺口 | 收据原文 |
|---|---|---|
| S2 | 「回流罐」置信度 0.27 < 阈值 0.34 被跳过（映射 cylinder_vessel） | 「回流罐」最高置信度 0.27 低于阈值 0.34（cylinder_vessel），已跳过。 |
| S3 | 「在 X 的出口加仪表」子句语法不存在 | 无法理解的子句：「在 P-101 的出口加一台压力表 PI-101」; 无法理解的子句：「在 P-101 的出口加一台温度计 TI-101」 |
| S4 | level_gauge 双口仪表挂接语义缺失（no_instrument_land_port） | 「LG-101」挂接于 V-101：no_instrument_land_port（level_gauge 没有可承接挂接的 process 口（双口仪表的挂接语义需单独设计）），挂接未建立。 |

## 结论

判读式链路在 S1/S5/S6 上端到端成立且双 run 逐字节一致；S2/S3/S4 的拒识全部带具体收据（无无声失败）。三个新发现 gap 均为小切口（别名置信度/一条子句语法/一类仪表挂接语义），适合 DI-1-D4 按 D2-3 授权逐项修通。
