# M9 Closeout · Construction-grade Delivery Candidate（草案骨架 — DRAFT）

> 状态：**DRAFT，不可用于签 Closeout Gate**。WS1/WS2 已冻结封账，本文档先固化已闭合事实；WS3B 真实试点与最终结论段保持 PENDING，待 Owner 选定试点对象、Gate 单独放行 WS3B 后填写。

## 1. Milestone 状态总表（已核实事实）

| 工作流 | 状态 | 落点 | 证据 |
|---|---|---|---|
| M9-WS1 review workflow | PASS / CLOSED | PR #67 squash → main@153c28c | Gate Round-2 SQUASH MERGE GO；approval reconcile/token loopback/resolve 状态机/surface contract 四项 CLOSED |
| M9-WS2 release gate + evidence package | PASS / CLOSED | PR #68 squash → main@7d5cfa3 | Gate MERGE GO（绑定 a091728 / CI 36546392485）；R49-1~6 + 11 amendments + R68-1~5 全闭合；main CI 36548626197 四绿 |
| M9-WS3A pilot prep | GO（模板已交付） | reports/m9-ws3a-pilot-template.md（docs 分支） | 等 Owner 填表选定真实试点对象 |
| M9-WS3B real pilot | PENDING | — | 需 Gate 单独放行 + Owner 输入；放行时重新确认 main exact SHA 与运行环境 |
| M9 Closeout | PENDING（本文档） | — | 待 WS3B 结论回填 §5/§6 |

## 2. 交付能力总结（WS1+WS2 已冻结语义）

- **治理面与工程面正交**：review/release 全部治理 mutation 走 governance_seq CAS 单平面，永不移动 engineering revision/digest（硬测锁定）。
- **审查闭环**：线程（open/resolved/reopened 状态机）+ 评论 + 人类 approval（决策时 fresh readiness）；统一 reconcile 使任何 review surface 移动或 revision 漂移都让 live approval 失效，失效与 `approval.invalidated` audit 同事务。
- **人类权威边界**：operator token 仅 loopback 有效、constant-time 比对、shared 部署 fail-closed；agent 可请求/评论但永不决策。
- **正式发布**：两态 ReleaseRecord 双绑定（revision + review snapshot digest）；守卫顺序冻结（actor→threads→approval→唯一性→fresh readiness→build）；Phase A 单 pinned snapshot 内存构建证据包、Phase B `commit_release` 单事务原子落库（revision 复核+CAS+state+BLOB+audit）。
- **取证包**：七成员 + MANIFEST.sha256，hash 形成顺序消环（release.json 为 evidence projection）；audit cutoff 为单快照验证的历史锚点（含 database_instance_id）；导出前五重完整性校验 + 跨面双 hash 绑定，全部内存完成；corrupt 统一 500。
- **审计**：review/release 事件 10 个封闭 Literal；denied 为 audit-only（F4 不可信零注入、竞争零 denied）；audit 全局 hash chain 不变。
- **存储**：schema v13（additive `release_evidence_packages`，无 FK 级联）；v12 旧库零迁移成本直读。

## 3. 验证资产

- 后端契约测试：WS1 13 条 + WS2 15 条（含 Barrier 真并发、跨面包移植、MANIFEST 逆序突变等硬锁）；surface contract 机器锁；迁移锁。
- 全量回归：1692 passed + ruff 净（M9 收口时需在 final main 上复跑并记录 exact run）。
- e2e：review/release 全链（approve→release→下载→sha256 回核）+ engineering-graph tab 计数。
- CI 门：Backend（Ruff/quality/Pytest）/Frontend/M5 deterministic/Chromium 四绿。

## 4. 不变量清单（P0 核对表，Closeout 时逐条打钩）

- [ ] semantic-first / tool-mediated editing / deterministic validation 未被 WS1/WS2 改动（白名单核对：store 工程写路径、PDF/DXF 内核、release_validator.py 生产语义零改动）
- [ ] audit/rollback：治理面每次 mutation 均有对应 audit 且可回滚验证
- [ ] model-agnostic：本里程碑无 provider 相关改动
- [ ] 人工正式审批边界：release/approval 只能由 operator token 在 loopback 触发（F4 硬锁）
- [ ] 迁移纪律：v12→v13 additive，旧库零证据包直读（迁移锁测试）

## 5. WS3B 真实试点（PENDING）

待 Owner 从 `reports/m9-ws3a-pilot-template.md` 选定试点对象后填写：试点图纸/场景、执行步骤、产出证据、试点结论（construction-grade 判定输入）。

## 6. Closeout 结论（PENDING）

待 WS3B 完成后：milestone 判定、遗留 deferred 项清单（预期含 M8 遗留 LIT 双 tap / HOLDOUT-1 / G4 等，与 M9 无关项区分）、Charter milestone 状态更新建议。

## 7. 复跑清单（签 Gate 前执行）

- [ ] final main exact SHA 确认 + 全量后端回归 + ruff + 前端构建 + e2e + CI run 编号回填
- [ ] Gate 会话路由裁定（closeout 预备受认可与否）
