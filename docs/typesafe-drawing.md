# TypeSafe 判读式画图（key 配置 + `typesafe-plan`）

TypeSafe 的 System One（本题用 `jev-latest`）不生成文字，它给出**带概率的判断**。所以这条路线不是
“让模型写一张图”，而是：**代码列出候选，模型只回答“指的是哪一个”，代码再落图**。图纸里多画一台不存在的
设备比少画一台更糟，判断式接口正好把这件事变成一个可以设阈值的数字。

## 1. 配置 API Key（界面）

Agent 面板 →「模型服务与高级设置」→ 新增的 **TypeSafe** 区块：

| 字段 | 默认 | 说明 |
|---|---|---|
| 用 TypeSafe 判读并画图 | 关 | 开启后本面板的生成请求改走 `typesafe-plan`，预览/应用流程不变 |
| TypeSafe API Key | 空 | 会话级保存（`sessionStorage`），与既有的服务访问令牌同一策略：刷新不丢、关浏览器即失效、不落盘 |
| TypeSafe Base URL | `https://api.typesafe.ai` | 偏好存 `localStorage`（非机密） |
| TypeSafe 模型 | `jev-latest` | 同上 |
| 测试 TypeSafe Key | — | 真发一次判断（`noul`），所以“配了 key”和“key 能用”不是同一句话 |

服务端也可以完全不传 key：`TYPESAFE_API_KEY` / `TYPESAFE_BASE_URL` / `TYPESAFE_MODEL` 是回退。
两处都没有时接口返回 400 `typesafe_key_missing`，**不会**退回任何默认 key——否则判断会来自一个用户没选的账号。

## 2. 一次画图是怎么走的

```
用户：新增一台燃料盐泵，把缓冲罐接到分离塔
        │
        ├─ 代码：切成子句 → 「新增」/「连接」/「未知」            (split_clauses)
        ├─ 代码：按图纸与符号目录列候选
        │        · 「泵」→ pump/blower/compressor/fan 家族符号      (candidate_symbols)
        │        · 图纸上已有的设备两两组合，各取一个空闲端口        (connection_candidates)
        ├─ TypeSafe：每个子句一个 Choice 问题，一次请求全部问完
        │        · criteria = 候选本身，state 只放候选的名字，不放 key
        ├─ 代码：置信度 < 0.34 → **跳过并写进说明**，不猜
        └─ 代码：选中项 → AddElementOperation / ConnectPortsOperation → 同一个编译器 + apply-v2 harness 闸门
```

要点：

- **候选由代码给**：模型只能在候选里选，选到候选外的值会被拒绝（`_select_symbol`），所以“模型幻觉出一个元件”
  在结构上不可能发生。
- **能查的不问模型**：子句里写了图纸已有的位号（`T-101`），那是查表，不是判断；只有歧义才交给 TypeSafe。
- **位号与管径来自原文**：`TAG-P101` / `DN50` 是从用户句子里正则取出的，模型不负责生成标签。
- **几何由代码定**：新设备放在已有图形右侧，逐台排开；判断模型看不到画布，让它决定坐标只会撞上质量门。
- **不确定性是输出**：低置信度子句进 `plan.explanation` 的 `[跳过]` 行，前端在预览里直接可读。

## 3. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v2/provider/typesafe/status` | 是否具备调用条件；只报 key 的**有无**与来源，不含 key |
| POST | `/api/v2/provider/typesafe/verify` | 发一次真判断；缺 key → 400 `typesafe_key_missing` |
| POST | `/api/v2/documents/{id}/agent/typesafe-plan` | 判读式画图，返回与 `plan-v2` 相同的 `SemanticAgentPlanResult` |

`typesafe-plan` 的产物走 `apply-v2`，也就是和其他计划**同一条**编译、评估、审批、审计路径。
两个 POST 都已在 `surface_contract.py` 登记（`runtime` / `agent_runtime`），符合 §7「没有未登记的写路径」。

## 4. 边界

- 目前支持两类子句：**新增设备**（从符号目录选型）与**连接两台已有设备**（选空闲端口）。其它子句进
  `unknown` 并在说明里列出，不静默丢弃。
- 需要 key、需要出网；离线环境只能用 `plan-v2` 的模型路线。
- 判断质量要在**你自己的图纸**上验证：阈值 0.34 是起点，不是结论；`semantics_candidates.json`
  （`reports/pid-repro/`）里已有的 112 个标注判读显示模型在真不知道处会说不知道（`干净` 0.29、`预留接口` 0.32）。
