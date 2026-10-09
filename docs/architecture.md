# P&ID-Agent 架构

> **现状对账（2026-10-09，M6-2B-D1）**：本文档描述的是**单文档内核**（document/transaction/history/
> planner 分层），该内核至今保持有效。自 M10 起仓库已扩展为多域治理架构，本文未覆盖的部分以
> 各里程碑封账文档为准：`runtime/` 中性端口与 Harness（M10）、`documents_registry` 与 Cable 第二域
> （M11）、项目图谱/工程链接/readiness/确定性交付包（M12，`reports/m12-closeout.md`）、受治理多域
> 变更集（M13，`reports/m13-closeout.md`）、M6 治理化语义摄取（Phase-1/2A 契约与实现，Phase-2B
> 设计基线 `reports/m6-phase2b-design.md`）。下文的「单一文档真相源」原则在多域架构下仍然成立：
> 所有工程写入最终仍归于 `DocumentService.apply_transaction` 一条受治理路径。

## 设计原则

### 单一文档真相源

网页、REST、MCP 和内部 LLM 规划器不能分别维护一份状态。所有修改最终都必须转成 `TransactionRequest` 并由 `DocumentService` 执行。

这样可以保证：

- 浏览器移动设备或连接节点后，Agent 读取到的是同一份最新文档；
- Agent 生成期间有人修改文档时，旧 revision 会被拒绝；
- 撤销、重做和审计可以覆盖所有修改渠道；
- 后续增加插件或 Python SDK 时不需要重写状态逻辑。

## 分层

```text
┌─────────────────────────────────────────────┐
│ React 编辑器 / REST 客户端 / MCP 客户端     │
└──────────────────────┬──────────────────────┘
                       │ TransactionRequest
┌──────────────────────▼──────────────────────┐
│ FastAPI / MCP adapters                      │
│ 参数转换、HTTP 错误、工具描述               │
└──────────────────────┬──────────────────────┘
                       │
┌──────────────────────▼──────────────────────┐
│ DocumentService                             │
│ 原子事务、revision、撤销重做、拓扑和场景摘要 │
└───────────────┬─────────────────┬───────────┘
                │                 │
┌───────────────▼────────┐ ┌──────▼───────────┐
│ SQLiteDocumentStore    │ │ SymbolRegistry   │
│ 文档与历史快照          │ │ 单位图例与连接口  │
└────────────────────────┘ └──────────────────┘
```

## 文档模型

`Document` 包含：

- `revision`：单调增加的并发版本；
- `canvas`：图幅、网格和背景；
- `layers`：可见性和锁定；
- `elements`：基础图元、设备符号、连接节点和语义管线；
- `metadata`：项目级扩展信息。

### SymbolElement

设备实例只保存：

- `symbol_key`；
- 位置、宽高和旋转；
- 位号或标签；
- 工艺属性。

具体形状和端口由 `SymbolRegistry` 提供。更换单位图例不会导致已有文档结构失效，只要保持 `symbol_key` 稳定。

### JunctionElement

连接节点表示真实的管线分支或汇合拓扑，而不是视觉上的线段交叉。节点提供固定的 `node` 端口，多条管线可以同时连接到该端口。

在编辑器中把连接节点放到既有管线上时，原管线通过一个原子事务拆分为两段，并分别连接到新节点。之后新增支路也连接到同一节点。移动节点时，所有关联管线同步更新。

### ConnectorElement

工艺管线不是普通直线。它保存：

- 正交折线点；
- `orthogonal`、`manual` 或 `direct` 路由模式；
- 可选 source/target 设备端口或连接节点；
- 管线或工艺标签。

这使 Agent 能理解“V-101 的出口连接 P-101 的入口”，也能理解“三条管线在 J-101 汇合”，而不只是看到互相重叠的线。

## 事务

事务包含一个或多个操作：

- `add_element`
- `update_element`
- `delete_element`
- `add_layer`
- `update_layer`
- `delete_layer`
- `clear_document`

执行过程：

1. 加载当前文档；
2. 检查 `expected_revision`；
3. 在内存副本上执行全部操作；
4. 验证图层、符号、端口、连接节点和正交路由；
5. 重新进行 Pydantic 文档级验证；
6. 写入新的 revision，并把旧快照压入 undo stack；
7. 任一操作失败则不保存任何变化。

## 历史

Alpha 阶段使用完整文档快照，优点是正确性高、实现简单，适合当前文档规模。后续文档变大后可以迁移为事件日志或增量 patch，而不改变 API 事务语义。

## LLM 规划器

LLM 不直接写数据库，也不生成任意代码。它只负责输出 `TransactionRequest`：

- 系统提示包含图例目录；
- 系统提示包含 Pydantic 生成的 JSON Schema；
- 输出再次由 Pydantic 验证；
- 图例 key、端口、连接节点、图层和 revision 由服务层再次检查；
- 写入失败不会留下半张图。

## 兼容策略

发行包、命令、界面和服务名称使用 `P&ID-Agent` / `pid-agent`。Python 模块路径 `agentcad`、旧 CLI 和 `AGENTCAD_*` 环境变量暂时作为兼容别名保留。

## 扩展方向

- 文件知识库应作为独立的 project context 层加入；
- 自动布局应生成事务，而不是绕过服务层；
- DXF/PDF 应作为 document exporter；
- 单位规则检查应读取同一语义文档并返回可定位到 element id 的问题。
