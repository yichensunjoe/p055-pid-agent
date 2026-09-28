# M8-Q2 Catalogue Correction / Expansion · 收口报告

> 签署：M8-Q2 OVERALL GATE = PASS / CLOSED（Gate 会话「方案落地顺序裁决」2026-09-29 裁定）。
> 最终基线：**main = `7f39d92`**（#56–#65 十个 squash PR 全合，#56–#65 各 PR exact-head CI 四绿）。
> 过程设计文档（批间往返全记录）：`reports/m8-q2-design.md`（文档分支 `m7-semantic-first-synthesis`）；本文件是 main 线上的封账 artifact。

## 一、各子阶段最终状态（全部 CLOSED）

| 子阶段 | PR | 内容 | 状态 |
| --- | --- | --- | --- |
| Q1R Measurement Integrity | #56 | P1 B' 预提交对账（拒绝零副作用）、G7 route parity（端口锚点规范化）、G2 仪表枚举分解、系统声明 fail-closed receipt | CLOSED |
| Q2-2(a) condenser hint | #58 | SYMBOL_HINTS 精确组（冷凝/冷凝器/condenser）→ 候选恰好 [condenser]；DEV-6 全三句 complete | CLOSED |
| Q2R1 selector-ID remap | #57 | edit 连接重铸后 selector 随连接重键（DEV-6 串台根因） | CLOSED |
| Q2-1R2 TT visibility | #59 | temperature_transmitter 单行解禁 + EXACT_DEVICE_HINTS 精确短语优先；FT/LT 保持 hidden | CLOSED |
| Q2R2 embedded-add | #60 | connect 子句内嵌设备声明（量词强制 + existing_tags），DEV-6 全链 | CLOSED |
| Q2R2.1 label pool | #61 | questions(label_entities=pool)：跨 base 端点连接 judgment 不再 KeyError | CLOSED |
| Q2R3-A semantic attachment | #62 | DiagramEntity.host_engineering_id + 四 invariant + planner 宿主解析 | CLOSED |
| Q2R3-B1 resolver | #63 | 受治理 tap resolver + 四类 reason 收据（未知类型不猜）+ 宿主删除守卫 | CLOSED |
| Q2R3-B2 tap 物化 | #64 | catalogue tap 口 + carrier 传送带 + 物化 tap 行 + 介质边界 + LIT/物化双 fail-closed | CLOSED |
| Q2R3-B3 零连接窄批 | #65 | attachment-only 图放行 routing（普通无连接图仍拒） | CLOSED |

## 二、冻结的 fail-closed 契约（本批闭合，未来不得静默绕过）

1. **拒绝零副作用**（P1 B'）：任何 4xx/rejected 路径 store 状态逐字节不变（precommit_validator 在 store.save 前对同一个 working document 对账）。
2. **route parity**（G7）：canonical layout 的 connector 端点 = 精确端口锚点（drafting_geometry 唯一公式），writer 保存即 layout 决定。
3. **selector 不猜**：歧义必 receipt；匹配器自解析契约（name/alias→self、多命中→显式 ambiguity）。
4. **未知能力诚实**：系统声明/未知语法 → structured receipt + partial，绝不 mint phantom 设备或假 complete。
5. **未知仪表类型不猜**：resolver 四类 reason（no_governed_tap_port / unsupported_instrument_type / no_matching_governed_tap_port / multiple_governed_tap_ports），单一 tap 也不默认选。
6. **介质边界**：process 端点（隐式+显式+selector）只认非 instrument-medium 口。
7. **attachment 全链 fail-closed**：语义关系存在则物化必须兑现，任何缺失（host 事实/放置/绑定/注册表/resolver/land 口/锚点）→ MaterializationError，无 silent-drop。
8. **宿主删除守卫**：有存活挂接仪表的宿主拒绝删除 + receipt（attachment_host_has_dependents）；连从属同删允许。
9. **测量基线自证**（P0-RUNTIME-PROVENANCE）：runner corpus lock（source commit/blob/17 步硬断言）+ provenance 证据前捕获。

## 三、关键场景终态

- **DEV-6**：全三句 200/complete（塔区多端口 + 冷凝器 + 侧线采出全链）。
- **DEV-4**：诚实终态 = partial + 唯一 receipt（LIT `no_instrument_land_port`——level_gauge 的 upper/lower 双口语义需独立设计）；TT/PIT 静默解析。**此为已签署的能力边界，不是 blocker。**
- **HOLDOUT-2**：双 complete（并联泵 + 删除链）；HOLDOUT 全程只验收未反推。
- **G6**：CLOSED / NOT A DEFECT（侧位通用词表派生，非数据污染）。

## 四、deferred future scope（NOT AUTHORIZED，不得顺手实现）

1. LIT 双口双 tap 设计（level_gauge upper/lower 双挂接语义）。
2. HOLDOUT-1「换成」替换语法。
3. G4 泄放支路 router 绕障。

（system declaration 支持是 §2 第 4 条已记录的 fail-closed 能力边界，不属于 deferred engineering item。）

## 五、Digest / 版本基线

spec schema `m7-diagram-spec/3`、adapter topology `m7-adapter-topology-digest/3`、semantic layout plan `m7-semantic-layout-plan-digest/8`、buffer_tank 四口（in/out 原位 + tap_pt(35,0)/tap_level(70,38) instrument 类）、catalogue geometry golden 已按 B2 intentional bump 显式更新。
