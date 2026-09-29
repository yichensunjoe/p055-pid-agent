# M9-WS3B Shadow 预演报告（REHEARSAL — 非真实试点证据）

> 裁定依据：Gate（新会话）对候选 A 给出 **WS3B rehearsal / shadow dry-run GO**；同时明确「若 Owner 只确认拿 synthetic HOLDOUT-1 跑，仅放行 rehearsal，不能用于回填 M9 Closeout 的真实试点完成」。本报告即该 rehearse 的记录，**不构成 M9-WS3B REAL PILOT 完成证据**。

## 运行环境（preflight 全项）

| 项 | 值 |
| --- | --- |
| runtime 代码 | main@7d5cfa33b7faee4656efffff636fcdd83a68b83c（专用 worktree P055-PID-Agent-main，非 docs 线） |
| 服务 | uvicorn 127.0.0.1:8002，disposable SQLite（ws3b-rehearsal/rehearsal.db），operator identity=预演操作者 |
| schema | v13（迁移自动完成） |
| pilot 文档 | doc_4ddaf820f7a1「WS3B 预演 · HOLDOUT-1 小流程」 |

## 七步验收脚本执行结果（11 项检查全 PASS）

1. **建图**（人工编辑通道 POST /documents/{id}/transactions，非 fixture、非直写 DB）：HOLDOUT-1 句一 5 设备（V-101 缓冲罐 → P-101 离心泵 → E-101 双横向管壳式换热器管程 → FCV-101 调节阀 → V-102 缓冲罐）+ 4 连接（source/target 端口绑定、forward 流向、全局 y 对齐）；revision=1，9 图元。✅
2. **release-readiness**（GET /api/v2/validation/documents/{id}/release-readiness）：fresh 全绿（后续 approval/release 内部各重跑一次，均 eligible）。✅
3. **审查闭环**（模拟意见，标注【预演·模拟意见】）：线程提出 → resolved + resolution_note。✅（真实试点时此处只走真实意见，不模拟）
4. **工程批准**：agent 身份申请 → operator 决策 approved（决策时 readiness eligible 绑定）。approval = ap_cd6ad5fff256。✅
5. **正式发布 + 证据包**：release = rel_e4471572a037（state=released）；证据包 33 056 字节，**package_sha256 = 0cfef364bfa7b0e1c2183635d52979b257500214fbc899297e74ce29f738bae5 与 ReleaseRecord 写回值一致**；MANIFEST 七成员 + MANIFEST.sha256 全齐；审核链 cutoff / readiness-validation hash 锁定在包内。✅

产物：ws3b-rehearsal/rehearsal-evidence.zip、rehearsal-manifest.txt、rehearsal-result.json、rehearsal_driver.py（可复跑）。

## 预演结论

- 七步验收脚本在 main@7d5cfa3 实际运行时上一次跑通，WS1+WS2 全链（建图→validation→review→approval→release→evidence）行为与冻结契约一致。
- 脚本本身（ws3b-rehearsal/rehearsal_driver.py）经真实试点时只需：换真实图纸来源、审查意见改为 Owner 真实意见、登记表四字段补齐。
- 一项脚本修正记录：release-readiness 端点实际路径为 `/api/v2/validation/documents/{id}/release-readiness`（已回填试点候选文档）。

## 升级到 REAL PILOT 仍差的（Gate 冻结，Owner 动作）

①真实项目锚点 ②专业审核人姓名/角色/资格 ③≥1 项真实项目 reference ④数据授权登记。补齐自动转 EXECUTION GO。
