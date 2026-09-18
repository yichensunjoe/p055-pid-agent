# P&ID 确定性绘图质量

系统提示词只负责告诉模型“什么是好图”，真正保证稳定性的边界位于模型之后。P&ID-Agent
把生成过程拆成四层：

1. 模型先输出工艺拓扑、精确图例和真实端口，不直接决定最终像素路径；
2. 语义编译器校验图例、端口、介质和入口/出口关系；
3. 确定性路由器按端口外法线、障碍物、既有管线和画布边界选择最短正交路径，并为后绘制的
   相交管线设置跨线桥；
4. 完整新图在落库前运行图面质量门禁，不合格则把稳定 issue code 交给模型重规划。

## 排版合同

- 主工艺从左向右，进料/IN 在左，产品/OUT 在右；主线先于公用工程、仪表和文字确定。
- 使用用户要求的精确阀门图例；闸阀、截止阀、止回阀、调节阀、蝶阀、针阀和安全阀不能用
  通用球阀代替。
- 对齐的是端口绝对坐标，不是图例左上角坐标。已共线端口直接连接，不允许人为制造几像素
  的折线。
- 管线只能从源端口的外侧离开，并从目标端口的外侧进入。泵顶排口先竖直离开，再汇入水平
  管廊。
- 相交不等于连接；只有显式 junction 才能分支或汇合。不可避免的几何相交由后绘制/次要管线
  使用 jump bridge。
- 预留一致的设备间距和标注空间，禁止图例重叠、管线穿设备、重复标签和文字压线。

## 质量报告

`pid-agent.diagram-quality` v2 提供 0–100 分、是否通过、逐项指标和稳定 issue code。完整新图
必须达到 95 分且不存在 error。重点错误包括：

- `NON_ORTHOGONAL_SEGMENT`、`MICRO_SEGMENT`、`UNNECESSARY_BEND`；
- `PORT_DIRECTION_MISMATCH`、`PORT_EXIT_MISMATCH`、`PORT_FACING_MISMATCH`；
- `PIPE_THROUGH_EQUIPMENT`、`UNBRIDGED_CROSSING`、`CONNECTOR_OUT_OF_BOUNDS`；
- `NODE_OVERLAP`、`DUPLICATE_LABEL`、`ANNOTATION_OVERLAP`；
- `QUALITY_SCORE_BELOW_TARGET`。

## 与 M3 确定性整理的关系

`pid-agent.diagram-quality` 是**图面规则**（“这张图画得好不好”），也是 M3 整理引擎的评分与
错误码来源；M3 的 `pid-agent.drafting-report` / `drafting-preview` 则是在同一套评分之上做
**确定性修正**（重新排布、重路由、标签摆放、跨线桥、保留区、碰撞分离），并额外回答三个这
里不回答的问题：改动是否可复现、是否严格不改差、是否越出了请求的范围与锁。两者共用同一份
评分，但一个是判据、一个是手术刀，具体契约见 [`deterministic-drafting.md`](deterministic-drafting.md)。

运行 `pid-agent quality-harness` 可在不调用模型的情况下验证这条边界。真实模型验收使用
`pid-agent model-matrix --include-complex-diagram`，复杂场景除拓扑外还必须通过完整图面质量
报告。
