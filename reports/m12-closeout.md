# M12 Closeout — Multi-Domain Project Graph & Deterministic Delivery Package

> Gate 口径：M12 DEFINITION APPROVED（Charter v1.1.0 §55B）；D1–D4 已签 CLOSED，D5（本 PR）待签 Merge Gate。
> 本矩阵按 §55B 七项完成标志逐项给证据。治理声明与 M11 相同：本 closeout ≠ M9 WS3B closeout、
> 不补齐真实项目试点、不改变 M9/M10 Owner 阻断状态、不产生 deploy 授权。

## 1. Domain-neutral Project Graph — D2（PR #78）

- v15 新增 `projects` / `project_documents` / `engineering_links`；`_migration_15` 单事务（FK ON）+
  backfill：恰好一个默认项目拥有全部 registry 文档；失败显式 rollback（注入故障测试）。
- 成员身份 = {document_id, domain}，domain **一律 join documents_registry 得出**（成员表无 domain
  列，假身份不可表示，R77-Q1）；`document_id UNIQUE` = 单 active project 归属。
- 稳定 project_id（`proj_m12default` 默认项目 + `create_project` 生成式）。
- 备份/回滚链冻结并测试：v14 binary 备份 → v15 迁移 → restore 得 v14 → 再迁 v15；
  `restore_backup(allow_pre_current_schema=True)` 的 legacy 分支要求 actual==metadata schema
  且过 required-schema 门（两个篡改负例）。

## 2. Cross-domain Engineering Link — D3（PR #79）

- 第一种真实关系 `cable_endpoint_equipment`（cable 段指定端端接于 P&ID 设备元件），显式
  object_ref 建立，禁止名称推断。
- 六条 relation invariants 全部写前 fail-closed（orientation/endpoint/成员归属/pin==当前/
  active endpoint 部分唯一索引/软删除）；equipment predicate = symbol category ≠ 仪表类目
  （本地常量由测试钉住 canonical 值）。
- delete canonicalize 只验身份 = 坏 link 的可恢复清理路径（R79-F1）。

## 3. Project-level deterministic validation — D4（PR #80）

- 七 stable issue code（dangling×2/missing×2/wrong_domain=blocker；stale×2=warning 且
  fail-on-warning）；wrong_domain = 声明 vs registry 真值（D80-1）；missing_source_object =
  段存在 + endpoint 合法双条件（D80-2）。
- stale-first：stale 端只报 stale，不声称检查历史 pinned 对象；soft-deleted links 不参与；
  不存在的 project fail-closed（project_not_found，D80-3）。
- P&ID 成员 readiness 唯一来源 `assess_document_release_readiness(service, id,
  load_profile(), now=evaluation_as_of)`（服务器侧 profile）；Cable = `assess_cable_document()`。
- project eligible 精确定义：全成员 eligible 且 failing rule=0；只 eligible/not_eligible，
  模型层无 approval/release 字段。
- F77 provenance：result_hash 绑定 evaluation_as_of + 成员 readiness_hash + issues + active
  pins + profile fingerprint；profile 漂移→成员 hash→项目 hash 传播有 hard-lock 测试。
- 纯只读：测试断言 assess 前后 audit 记录数零增长。

## 4. Governed project mutation — D3（PR #79）

- link 三操作唯一写路径 = M10 runtime：中性 tool（permission=ask, risk=engineering_change）
  → session/approval/authorization（tool_intent_hash 锁批准状态）→ adapter → service →
  store `commit_engineering_link_*`（BEGIN IMMEDIATE 单事务：mutation + governance audit +
  tool-call closure + approval consumption + session closure）。
- TOCTOU 三层：canonicalize 服务器注入 expected revisions → apply 重 canonicalize 与授权
  hash 比对（revision_conflict 零写入）→ 事务内 current==expected CAS；全量不变式写连接上重验
  （membership 移除竞态测试、授权后删除竞态测试）。
- 无 actor 旁路写；授权后一切失败 → failure closeout（稳定码 + failed tool call + failed
  session + 恰好一条 rejected 审计）；endpoint 冲突映射只看部分唯一索引列组合。
- audit：新增 4 个 governance 事件类型（created/repinned/deleted/rejected），接既有 hash
  chain，formation/ordinal 语义未变（chain verify 测试全绿）。

## 5. Deterministic Project Delivery Package — D5（本 PR）

- 输入 {project_id, member pins, evaluation_as_of}（D81-1）：pins 为调用方声明的交付状态
  （CAS 边界），服务器逐一重读当前 revision 比对——current exact revision only，
  绝不历史重建（F77-2）；pins 集合≠成员集或任一 pin≠当前 → 稳定 409 不产包。
  UI 先从 summary 取当前 pins 再携带下载，两步间 revision 变动即 409。
- 硬前置（各为稳定错误且不产包）：project_not_found / empty_project /
  member_revision_not_current / link_pin_not_current。
- 冻结成员序（Q6，D81-2）：ZIP 最前 MANIFEST.json（members 不含自身），其后
  project.json → links/engineering_links.json（active，含两端 domain 声明，D81-3）→
  readiness/project_readiness.json → domains/pid/<id>-r<rev>.json（envelope 剥离墙钟，按
  document_id 排序）→ domains/cable/<id>-r<rev>.zip（D3 导出字节，按 document_id 排序）。
  verifier 对「集合+顺序」双校验，顺序漂移即 tamper_detected。ZIP_STORED + 1980 ZipInfo +
  canonical JSON。
- 一致状态快照（D81-4）：active-link canonical 快照一次捕获，ZIP 写出前最终重验成员
  revision 与链接快照，任何漂移 → package_state_changed 稳定 409，绝不产内部不一致包。
- verify 纯函数：成员 hash、缺/多/duplicate 成员、MANIFEST self-listing、成员顺序漂移、
  ZIP metadata 漂移（含字节翻转 CRC）全部 tamper_detected（缺/多/重复/顺序各有独立测试）。
- 字节 parity：同 pins+as_of+profile 重建逐字节相等；e2e 三路 parity
  （HTTP body == UI download == fresh-process 直接构建）。

## 6. Minimal project inspection surface — D5（本 PR）

- 只读 GET：`/api/v2/projects/{id}`（成员+revision）、`/links`（active）、`/readiness`
  （evaluation_as_of 必填 tz-aware，否则 422）、`/package.zip`（显式 pins 参数）。
  零 audit：四个 GET 前后 audit 计数相等有 hard-lock 测试（D81-5）。
- 最小 UI：第三个 domain tab「项目」——成员表、跨域 links、readiness（可编辑
  evaluation_as_of + 重新评估）、交付包下载（错误如实显示，如 409）。
- shared-mode：匿名四族 GET 全 401，带 token 可用（security.shared.spec.ts）。

## 7. 全量非回归

- backend：1809 passed + ruff 净（pre-M12 基线 1754；净增 55 个新测试，零删测零放宽）。
  实现证据 = code head 77e0cd1 / CI run 37191981718；若随后仅有 closeout 文档机械修正，
  该 run 即 code-identical implementation evidence，不称后续纯文档 commit 的 exact-head run。
- frontend：npm test 163 pass。
- CI 四 job 全绿：Backend / Frontend / Chromium（local e2e 含 project spec）/
  M5 72-case deterministic gate；shared-mode security acceptance 在内。
- 视觉基线：darwin 本机随第三 tab 再生；linux committed baselines 在 exact-head CI 重新
  断言通过（第三 domain tab 属冻结 app-shell 变化的机械性更新）。
- 性能对账（PERFORMANCE EVIDENCE METHOD AMENDMENT，Gate 已批）：同一 GitHub Backend
  workflow / Python 3.11 / pytest 命令的 exact logs 对比——pre-M12 基线 274.87s（run
  36791335881，1754 passed）→ code head 77e0cd1 / run 37191981718（178.46s，1809 passed）：
  约 -35%，≤ +10% 预算内（阈值未放宽；实现证据以该 code run 为准，见非回归节注记）。

## 治理与遗留

- 治理声明（不变）：M12 closeout ≠ WS3B 真实试点；M9 Closeout 未完成；M10 FINAL ACCEPTANCE
  的 WS3B/M9 chained condition 未解除；无 deploy 授权。
- 未做（按冻结禁项）：第三 domain、Domain SDK、auth redesign、新 approval/release 状态、
  audit hash formation 变更、历史 revision 包、package 下载审计化（未来 governed POST milestone）。
- 多项目 UI、跨项目共享留未来 milestone（D1 Q1 冻结）。
