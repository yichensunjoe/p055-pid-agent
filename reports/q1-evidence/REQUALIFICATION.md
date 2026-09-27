# M7-Q1 Requalification（DXF-Q1 repair 后）

- 基线：main = ba4d447c04310398cab73af0c0875c9a1144f231；修复线 review/dxf-q1-repair = f79a4c9（1 个纯后端 commit，exporter-only，按裁决不 bump 任何 digest 版本）。
- 重验方法（按你的最小 requalification）：保存的 Q1 r2 真实文档（data/q1-qualification.db 中 doc_fda007951126，r2，来自真实模型 run）→ 修复后的导出器 → DXF 成功 → 仓库自带 read_dxf 读回 → 存证。
- 产物：r2-drawing-repaired.dxf · SHA-256 26d5a795b6efda398a18d436c5d94d7d8e1a852dc67b0bf2ae287bebb3ff0dd5
- 读回：primitive 种类 {line:3, circle:3, polyline:4, fill:2, text:3}；最大轮廓 34 顶点（采样弧线，非包围盒）。
- 结论：原 Q1 r2 的 downstream DXF 失败已闭。历史保留：ba4d447 真实资格测试 DXF FAILED；f79a4c9 同一下游用例 CLOSED。
