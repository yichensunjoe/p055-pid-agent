# DI-1-D4 证据：实现片后的冻结语料复跑

对照基线：`reports/di1-latency-baseline.json`（D3，修复前）。本次：`reports/di1-d4-corpus-evidence.json`（同一 runner、同一冻结语料 S1–S6、同一 sha256、双 fresh-process run）。runner 只有一处变更：收据采集补齐 port_ambiguities 与 quality 字段。

## 场景结果对照（run 1；run 2 逐一致）

| 场景 | D3 基线 | D4 后 | 变化归因 |
|---|---|---|---|
| S1 泵罐进出料 | committed | committed | — |
| S2 塔顶冷凝回流 | 422 partial（「回流罐」置信度 0.27 被跳过） | 422 port_ambiguity（**前进了一步**） | F-a 别名生效：回流罐已解析为 horizontal_vessel；剩余拒绝是「把 E-101 的工艺出口接到 V-102」的 target 端口未点名——horizontal_vessel 有 in/top 两个可承接 process 口，回执按 M7-Q2 契约列出两口及各自 selector（入口/顶部管口），绝不猜 |
| S3 泵出口压力/温度指示 | 422 partial（子句不理解） | **committed** | F-b 语法 + 泵 tap_pt 端口（F-c 目录侧） |
| S4 液位指示+控制阀 | 422 partial（no_instrument_land_port） | **committed** | F-c 双口仪表声明落点（level_gauge→lower） |
| S5 串联闸阀→改控制阀 | committed → 422（「换成」不理解） | **committed → committed** | F-e 替换语法；身份/位号/连接全保留 |
| S6 进料-精馏-冷凝-回流 | 三句 committed | 三句 committed | — |
| G4 泄放支路探针 | 拒绝（router 白名单） | 拒绝（不变） | F-d 未授权，正确保持 |
| TT-101 探针 | committed | committed | — |
| SYSTEM-DECL 探针 | 诚实拒绝 | 诚实拒绝 | 设计行为 |

## replay 与延迟

- deterministic replay：6/6 场景 spec_digest + outcome 双一致（与 D3 基线同口径）。
- 延迟分布与 D3 同量级（每步亚秒至 ~1.5s；修复不引入数量级变化），详见 JSON 双 run 样本。

## 生成回执质量门禁（F2 客观锚点）

每个 committed 响应携带 `quality`：`score / passed / target_score / blocker_count / issue_codes / engine_version`（drafting engine 只读分析，dry-run 与拒绝为空对象——没画图就没有分数）。本次实测：S6 首句 score 100 且 gate passed；S1/S5 有 DRAFT_MICRO_SEGMENT blocker、S3 有 DRAFT_TEXT_CONNECTOR_CROSSING blocker——如实记录，属布局引擎既有行为，不在 D4 修复范围。

## nl-source 只读端点

`GET /documents/{id}/nl-source` → `{available, revision, spec_digest}`；无源图纸 available=false；未知文档 404；GET 零审计写（测试断言审计面只有 document.created/revision.created）。前端在文档加载时用它恢复「改图」按钮可用态（修复刷新断链）。

## 剩余已知边界（诚实登记，均不在 D4 授权范围）

- S2 的 vessel 端口角色默认（多 process 口设备的未点名连接）需要自己的设计闸门，与 G4 同族。
- DRAFT_MICRO_SEGMENT / DRAFT_TEXT_CONNECTOR_CROSSING 等布局引擎既有 blocker 不属本片。
