# P&ID-Agent

P&ID-Agent 是一款轻量、专注于工艺流程图的浏览器 P&ID 软件。

它不是 AutoCAD 的通用替代品，也不计划加入三维建模、机械零件、BIM 或与 P&ID 无关的复杂命令。它只围绕一件事设计：让工程人员和 AI Agent 使用同一套单位图例、同一份结构化图纸和同一套连接语义，共同创建、修改、解释和检查工艺流程图。

> 当前版本：`2.1.0-alpha.1`
>
> 仓库 slug 的规范名称为 `PID-Agent`，产品显示名称为 `P&ID-Agent`。Python 导入路径暂时保留为 `agentcad`，避免已有客户端立即失效。

## Purpose

提供一个轻量、浏览器优先、可被人工和 Agent 共同编辑的 P&ID 文档系统；文档、连接拓扑、工程报告和 Agent 事务共享同一个后端真相源。

## 长期总体任务书

本仓库的长期 canonical 目标、不可变工程原则、Agent Harness 架构、P&ID Semantic IR、Tool Registry、权限审批、工程校验、交付门槛、里程碑和未来模型重规划规则，统一见 [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md)。

后续研发允许随着模型能力升级持续优化实现路线，但最终目标保持不变：让 AI 通过受控工程工具交付可验证、可审计、可进入正式工程审查和施工阶段交付流程的结构化 P&ID，而不是只生成“像 P&ID 的图片”。

## Status

WIP / Alpha — 核心文档引擎、浏览器编辑器、REST/Python/MCP 接入、导入导出和共享部署安全已形成完整实现；仍需按下方质量门槛持续验证，不能视为已完成的生产 CAD 替代品。

## Stack

- Python 3.11+、FastAPI、Pydantic、SQLite、CairoSVG。
- React 19、TypeScript 7、Vite 8、Zustand、SVG。
- MCP stdio、OpenAI-compatible Chat Completions。
- Pytest、Ruff、Node test、Playwright；可选 Docker Compose 部署。

## 产品目标

- 工程人员可以像使用轻量流程图工具一样自由放置设备、阀门、仪表和文字；
- 工艺管线连接到明确的设备端口或连接节点，而不是退化为无意义线段；
- Agent 可以读取最新设备、位号、端口、管线、分支和汇合拓扑；
- Agent 的修改经过 JSON Schema 和原子事务验证，并且可撤销；
- 单位图例使用声明式 JSON 维护，人工编辑器和 Agent 共用同一份符号定义；
- 最终支持生成和继续编辑与实际复杂 P&ID 相当的工程图纸。

完整产品边界见 [`docs/product-vision.md`](docs/product-vision.md)。Agent Harness 第一阶段的统一工具契约见 [`docs/tool-registry.md`](docs/tool-registry.md)，Session 与 Permission/Approval Gate 见 [`docs/agent-harness-sessions-permissions.md`](docs/agent-harness-sessions-permissions.md)。

## 当前能力

### P&ID 文档内核

- 文档、图层、图元、设备符号、连接节点和工艺管线统一模型；
- SQLite 持久化；
- 原子批量事务；
- document revision 乐观并发，防止 Agent 覆盖人工修改；
- 完整文档快照撤销和重做；
- JSON、SVG、PNG、标准图幅 PDF 与工程 DXF 导出；
- 版本化单文档 JSON 与原子项目包导入/导出，可在导入后继续编辑、撤销和重做；
- 从结构化图纸生成设备表、管线表、仪表索引和确定性工程规则检查；
- 场景摘要包含符号端口、连接节点和管线 source/target。

### 浏览器编辑器

- React、TypeScript、Vite、Zustand 和 SVG；
- 设备符号、基础图元、文字和工艺管线；
- 设备端口显示与吸附；
- 正交管线；
- 移动设备后关联管线自动保持连接；
- 单选、Shift 多选和拖拽框选；
- 多元素移动、删除和复制；
- `Ctrl/Cmd+D` 复制选择；
- `Ctrl/Cmd+A` 全选；
- 连接节点工具；
- 在既有管线上放置连接节点时，主管线原子拆分为两段；
- 支路可以吸附到同一连接节点，形成 Agent 可查询的真实分支/汇合拓扑；
- 选择管线后可拖动内部线段手柄，调整折线路径并保持正交；
- 中键平移、滚轮缩放和网格吸附。

### Agent 接入

- OpenAI-compatible Chat Completions 规划器；
- REST API；
- Python Client；
- MCP stdio Server；
- 适用于 OpenAI API、Ollama、LM Studio 及其他 OpenAI-compatible 服务；
- 模型只生成结构化事务，不能绕过服务层直接写数据库。

## Quick Start

要求 Python 3.11+ 和 Node.js 20+。

PNG/PDF 导出还需要 Cairo 系统运行库。Debian/Ubuntu 可执行
`sudo apt-get install libcairo2 libpango-1.0-0`；macOS 可执行
`brew install cairo pango`。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[mcp]"

cd frontend
npm ci
npm run build
cd ..

pid-agent serve --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000`。

开发模式：

```bash
# 终端 1
pid-agent serve --reload

# 终端 2
cd frontend
npm run dev
```

旧命令 `agentcad` 和 `agentcad-mcp` 暂时保留为兼容别名。

## 模型配置

```bash
export PID_AGENT_LLM_BASE_URL="http://localhost:11434/v1"
export PID_AGENT_LLM_MODEL="your-model-name"
export PID_AGENT_LLM_API_KEY="optional-api-key"
```

旧的 `AGENTCAD_LLM_*` 环境变量仍可使用，但新部署应使用 `PID_AGENT_*`。

支持任何兼容 OpenAI 接口规范的模型端点（包括本地 Ollama、LM Studio、私有网关或云端 API）。在 Web 界面的 Agent 面板中，输入 Base URL 与 API Key 后可点击「发现模型」动态拉取端点上部署的所有可用模型，或直接手动输入模型名称。系统不会绑定任何硬编码模型。

规划流程：

1. 后端读取最新文档、revision 和语义场景摘要；
2. 将单位图例目录与事务 JSON Schema 提供给模型；
3. 模型只返回结构化事务；
4. 后端重新验证图例 key、端口、连接节点、图层和 revision；
5. 整个事务一次成功，或完全不写入。

## Commands

```bash
# 后端静态与单元检查
pytest -q
ruff check backend
pid-agent quality-harness

# 前端单测、构建和浏览器验收
cd frontend
npm test
npm run build
npm run test:e2e
```

`npm run test:e2e:shared` 会验证共享部署路径；涉及真实 Provider 的生成能力要另做最小 completion，不能用“模型列表可见”代替。


## 共享部署安全

默认 `local` 模式保持现有单机行为。多人、容器或反向代理部署应配置：

```bash
export PID_AGENT_DEPLOYMENT_MODE=shared
export PID_AGENT_API_TOKEN="replace-with-a-long-random-token"
export PID_AGENT_CORS_ORIGINS="https://pid.example.com"
```

共享模式缺少 token 或使用不安全 CORS 时会拒绝启动，并默认阻止 Provider 访问回环、私网、链路本地和云元数据地址。企业内网模型必须通过 hostname/CIDR allowlist 显式开放。认证、Provider 网络策略、请求上限、反向代理和诊断脱敏说明见 [`docs/shared-deployment-security.md`](docs/shared-deployment-security.md)。

## SQLite 备份与恢复

数据库在启动时按顺序执行版本化 migration，并使用与文件路径无关的持久实例 ID。可在服务运行期间创建一致备份：

```bash
pid-agent db info --database /data/pid-agent.db
pid-agent db backup --database /data/pid-agent.db --output /backup/pid-agent.pidbak
```

恢复前应停止所有应用进程；备份包会在原子替换前校验格式、SHA-256、schema version、实例身份、SQLite 完整性和 foreign key：

```bash
pid-agent db restore --database /data/pid-agent.db --input /backup/pid-agent.pidbak
```

缺失、损坏或跨实例目标需要显式 `instance_id` 确认。Docker volume、灾难恢复流程和错误处理见 [`docs/sqlite-backup-restore.md`](docs/sqlite-backup-restore.md)。

本机 `data/*.db*` 当前权限已统一为 `0600`；本轮只加固文件权限，未打开、迁移或改写数据库。文件权限不是备份，重要实例仍需独立备份与恢复演练。

## 单位图例

内置图例：

```text
backend/agentcad/data/symbols.json
backend/agentcad/data/standard_symbols.json
```

前者保留历史 key 兼容，后者提供泵、风机、换热器、容器、过滤器、阀门、安全附件、管件、
排放边界和仪表等标准化扩展。分类、端口约定和验收方式见
[`docs/standard-symbol-library.md`](docs/standard-symbol-library.md)。

通过外部路径加载单位图例：

```bash
export PID_AGENT_SYMBOL_PATHS="/path/company-symbols:/path/project-symbols"
```

相同 `key` 的后加载定义会覆盖内置图例。结构说明见 [`docs/symbol-schema.md`](docs/symbol-schema.md)。

每个单位符号建议至少提供：

- 稳定的英文 `key`；
- 中文名称、分类和工程用途；
- 默认宽高和 SVG 基础形状；
- 可连接端口、端口方向和介质类型；
- 位号规则和可填写属性；
- Agent 可理解的使用约束。

## Python 接入

安装的发行包名称为 `pid-agent`，兼容导入路径仍为 `agentcad`：

```python
from agentcad.client import AgentCADClient

with AgentCADClient("http://127.0.0.1:8000") as cad:
    document = cad.create_document("压缩空气系统")
    cad.apply_transaction(document.id, {
        "expected_revision": document.revision,
        "operations": [
            {
                "op": "add_element",
                "element": {
                    "type": "symbol",
                    "symbol_key": "gas_tank",
                    "position": {"x": 180, "y": 160},
                    "width": 90,
                    "height": 140,
                    "label": "V-101"
                }
            }
        ]
    })
```

## MCP 接入

```bash
pid-agent-mcp
# 或
pid-agent mcp
```

通用配置示例：

```json
{
  "mcpServers": {
    "pid-agent": {
      "command": "pid-agent-mcp",
      "env": {
        "PID_AGENT_DATABASE_PATH": "/absolute/path/to/pid-agent.db"
      }
    }
  }
}
```

MCP 工具包括：

- `list_documents`
- `create_document`
- `get_scene_summary`
- `get_document`
- `apply_transaction`
- `list_symbols`

## API 概览

```text
GET    /api/v2/documents
POST   /api/v2/documents
GET    /api/v2/documents/{document_id}
DELETE /api/v2/documents/{document_id}?expected_revision={revision}
POST   /api/v2/documents/{document_id}/transactions
POST   /api/v2/documents/{document_id}/undo
POST   /api/v2/documents/{document_id}/redo
GET    /api/v2/documents/{document_id}/scene-summary
GET    /api/v2/documents/{document_id}/export.json
GET    /api/v2/documents/{document_id}/export-v1.json
POST   /api/v2/imports/document
GET    /api/v2/project/settings
PUT    /api/v2/project/settings
GET    /api/v2/project/export.json
POST   /api/v2/imports/project-package
GET    /api/v2/documents/{document_id}/export.svg
GET    /api/v2/documents/{document_id}/export.png
GET    /api/v2/documents/{document_id}/print-preview.svg
GET    /api/v2/documents/{document_id}/export-v2.pdf
GET    /api/v2/documents/{document_id}/export-v2.dxf
GET    /api/v2/documents/{document_id}/engineering-report
GET    /api/v2/documents/{document_id}/engineering-report/{kind}.csv
POST   /api/v2/documents/{document_id}/agent/generate
GET    /api/v2/symbols
GET    /api/v2/agent/tools
GET    /api/v2/agent/tool-schema
```

撤销和重做可通过 `expected_revision` 查询参数进行乐观并发校验；编辑器和 Python Client 会自动发送当前 revision，省略参数仍保留旧客户端兼容性。

运行后访问 `/docs` 查看 OpenAPI。

JSON 格式、冲突策略、原子失败语义、浏览器操作和 Python Client 示例见 [`docs/project-json-import.md`](docs/project-json-import.md)。

PDF 图幅、分页、标题栏、预览和 Python Client 用法见 [`docs/pdf-print-export.md`](docs/pdf-print-export.md)。DXF 图层、单位、坐标、XDATA 和 CAD 交换说明见 [`docs/dxf-export.md`](docs/dxf-export.md)。设备表、管线表、仪表索引、规则代码和 CSV/Python Client 用法见 [`docs/engineering-reports.md`](docs/engineering-reports.md)。

`/api/v1` 主要旧端点仍由新文档引擎提供兼容。

## Project Structure

```text
backend/agentcad/       # 文档模型、服务、API、CLI、MCP 与导出
backend/tests/          # 后端测试
frontend/src/           # React/SVG 编辑器
frontend/tests/         # 前端逻辑测试
frontend/e2e/           # Playwright 验收
data/                   # 本地 SQLite 运行数据（不入 Git）
docs/                   # 产品、架构、安全、交换格式和验收说明
scripts/                # 维护脚本
reports/                # 本地质量/模型运行产物
```

## Configuration

- `PID_AGENT_DATABASE_PATH`：SQLite 数据库路径。
- `PID_AGENT_SYMBOL_PATHS`：公司/项目外部图例路径，按顺序覆盖。
- `PID_AGENT_LLM_BASE_URL`、`PID_AGENT_LLM_MODEL`、`PID_AGENT_LLM_API_KEY`：OpenAI-compatible Provider。
- `PID_AGENT_DEPLOYMENT_MODE`、`PID_AGENT_API_TOKEN`、`PID_AGENT_CORS_ORIGINS`：共享部署安全边界。
- 旧 `AGENTCAD_*` 变量仅为兼容；新部署使用 `PID_AGENT_*`。

真实密钥只放在本地环境或秘密管理系统，不能写入数据库、项目文件、报告或提交。完整安全配置和网络 allowlist 见 `docs/shared-deployment-security.md`。

## Local Verification

本地可独立运行核心检查，无需依赖 GitHub Actions：

```bash
pid-agent quality-harness
pytest -q
ruff check backend
cd frontend
npm test
npm run build
npm run test:e2e
```

Playwright 安装、headed 模式、视觉基线更新和 trace 查看方式见 [`docs/browser-e2e-visual-acceptance.md`](docs/browser-e2e-visual-acceptance.md)。
无需模型或 API Key 的图例、拓扑和 Agent 事务验收见 [`docs/offline-quality-harness.md`](docs/offline-quality-harness.md)。
系统提示词、确定性端口路由、跨线桥和 95 分图面质量门禁见
[`docs/pid-drafting-quality.md`](docs/pid-drafting-quality.md)。

## 近期路线

1. 完善属性编辑、图层和系统显隐；
2. 增加管线折点增删、自动整理和跨线表达；
3. 增加流向箭头、介质、管径、颜色和线型面板；
4. 让 Agent 按自然语言执行局部移动、替换、删除和重新连接；
5. 导入单位图例及历史图纸知识；
6. 自动布局、避让和大型图纸性能优化；
7. 批量问题修复、项目级规则配置和企业报表模板。

## Notes

- Alpha 版本的数据库 schema、导出格式和 UI 仍可能演进；迁移前先备份并看版本说明。
- 默认 `local` 模式面向单机；共享部署必须启用 token、严格 CORS 和 Provider 网络策略。
- P&ID 连接语义、工程正确性和视觉共线需分别验收，不能只看截图。
- 项目不是通用 CAD、BIM 或三维建模工具。

## License

MIT
