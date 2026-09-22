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
| 刷新服务端配置 | — | 重新问一次服务端（见下面第 1.1 节） |

服务端也可以完全不传 key：`TYPESAFE_API_KEY` / `TYPESAFE_BASE_URL` / `TYPESAFE_MODEL` 是回退。
两处都没有时接口返回 400 `typesafe_key_missing`，**不会**退回任何默认 key——否则判断会来自一个用户没选的账号。

### 1.1 面板会说明「这次调用用的是谁的 key」

Key 藏在一个 shell 配置文件里（例如 `~/.zshrc` 的 `export TYPESAFE_API_KEY=...`）时，浏览器**看不见它**：
服务端由谁启动、有没有把那个变量继承过来，从页面上完全无法推断。这正是“明明配了 key，界面却说没有”的来源。
所以面板挂载时会问一次 `/provider/typesafe/status`，并把结果写在 Key 输入框上方：

| 服务端回答 | 面板显示 | 输入框提示 |
|---|---|---|
| `api_key_source = environment` | 绿色：`服务端环境变量已提供 Key（TYPESAFE_API_KEY）：下面留空就用它` | `留空即用服务端环境变量的 Key` |
| 有 key 但来自请求 | 绿色：将使用本面板填写的 Key | 常规提示 |
| 未配置 | 黄色：`服务端环境变量里没有 TypeSafe Key：在下面填入` | 常规提示 |

留空即用服务端的环境变量，是**有意**的回退顺序（请求 → 环境 → 报错），所以本机把 key 放在 `~/.zshrc`、
以交互式 shell 启动服务端时，面板什么都不用填就能画图。注意非交互式 shell（IDE、launcher、`nohup`）
**不会** source `~/.zshrc`，这种情况下要么在面板里填一次，要么给服务端显式传环境变量。

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

## 4. 验证（两种，缺一不可）

注入 transport 的单元测试能证明代码的分支，但证明不了“这个 key 真的能用、这些 criteria 模型真的看得懂”。
所以另有两项**真实调用**的验收，都不需要把 key 交给脚本——`TYPESAFE_API_KEY` 从环境读，输出从不含 key：

```bash
# 判读式画图的端到端：真判断 → 真事务 → 同一个 permissive 编译器
.venv/bin/python scripts/typesafe_live_acceptance.py            # exit 0 = 两个子句都被选中且事务编译通过

# 面板的端到端（先起一个服务端，key 在服务端环境里）
PID_AGENT_DATABASE_PATH=/tmp/ts-ui.db .venv/bin/agentcad serve --host 127.0.0.1 --port 8123 &
cd frontend && BASE_URL=http://127.0.0.1:8123 node scripts/typesafe-panel-check.mjs
```

最近一次真实运行（本机 CPython 3.12 + `jev-1.13.0`，文本证据 `reports/typesafe/live-acceptance.txt`；
面板截图 `reports/typesafe/panel-key-source.png` **不进版本库**，因为仓库 .gitignore 忽略 `*.png`）：

| 检查 | 结果 |
|---|---|
| `verify()` | `noul 0.97`，778 ms |
| 一次画图（新增一台离心泵 P-201，把 T-101 接到 T-102） | 2 个判断、候选 7 符号 / 1 连接组合、907 ms、两处置信度 1.00 |
| 事务编译 | `valid=True` `issues=none` |
| 面板 | 显示“服务端环境变量已提供 Key”、输入框提示“留空即用服务端环境变量的 Key” |
| 面板点「测试 TypeSafe Key」 | `Key 有效 · jev-1.13.0`（服务端日志 `POST .../typesafe/verify 200`） |

## 5. 边界

- 目前支持两类子句：**新增设备**（从符号目录选型）与**连接两台已有设备**（选空闲端口）。其它子句进
  `unknown` 并在说明里列出，不静默丢弃。
- 需要 key、需要出网；离线环境只能用 `plan-v2` 的模型路线。
- 判断质量要在**你自己的图纸**上验证：阈值 0.34 是起点，不是结论；`semantics_candidates.json`
  （`reports/pid-repro/`）里已有的 112 个标注判读显示模型在真不知道处会说不知道（`干净` 0.29、`预留接口` 0.32）。
