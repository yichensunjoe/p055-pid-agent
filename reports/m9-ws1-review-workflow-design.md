# M9-WS1 Review Workflow · 详细设计（报 Gate 签 WS1 CODE GO）

> 作者授权：M9 DESIGN GATE 已批（WS1 DESIGN GO / CODE BLOCKED pending 本设计）。
> 基线：main=`fbcadae`。设计输入：reports/m9-gap-analysis.md@5bb413c（文档分支，非 main 血缘——已按 Gate 提示作 provenance 记录）。
> 本文件只交设计，不写代码。

## 1. Data model（新增 `backend/agentcad/review_models.py`，纯 pydantic，无 I/O）

```python
class ReviewThread(StrictModel):
    thread_id: str            # "rt_" + sha256(document_id|anchor_revision|element_id|created_seq)[:12]——确定性
    document_id: str
    anchor_revision: int      # 创建时工程 revision，钉死
    element_id: str | None    # 可选；None = drawing-level 评论
    subject_text: str         # 创建时快照的元素名/位号（展示用，非绑定）
    status: Literal["open", "resolved", "reopened"]
    created_by: str           # 可信 actor id（见 §4）
    created_at: datetime
    resolved_by/at: str|None, datetime|None
    reopened_by/at: str|None, datetime|None
    seq: int                  # 治理面序号（见 §3）

class ReviewComment(StrictModel):   # append-only entry
    comment_id: str           # "rc_" + sha256(thread_id|entry_seq|body|author)[:12]
    thread_id: str
    entry_seq: int            # 线程内从 1 递增；append-only，永不改/删
    body: str
    author: str
    created_at: datetime

class EngineeringApproval(StrictModel):   # 与 ToolApproval 完全分离
    approval_id: str          # "ap_" + sha256(document_id|revision|request_seq)[:12]
    document_id: str
    engineering_revision: int # exact 绑定；变化即 stale
    readiness_snapshot_hash: str   # assess_release_readiness 的 readiness_hash 复用
    review_snapshot_digest: str    # 见 §5
    decision: Literal["approved", "rejected"] | None   # requested 阶段为 None
    status: Literal["requested", "active", "stale", "superseded"]
    requested_by: str         # 可为 agent（发起允许）
    decided_by: str | None    # 必须可信人类（见 §4）
    requested_at / decided_at: datetime
    invalidation_reason: str | None
```

治理面总容器：`ReviewState(document_id, governance_seq, threads: tuple[ReviewThread], comments: tuple[ReviewComment], approvals: tuple[EngineeringApproval])`，经 store 持久化（新表 `review_state`，与 `documents` 平级——**不塞进 StoredDocument/Document.metadata**，避免工程 revision 漂移）。

## 2. State machine

- **Thread**：`open → resolved → reopened → resolved …`（reopened 即 open 语义，单独状态值只为审计可读）。resolve/reopen 均由可信人类执行；每次迁移写 audit + 更新 review_snapshot。
- **Approval**：`requested →（decide）active | 隐含 rejected 终态`。`rejected` 落 decision=rejected 后 status 直接记 `stale`（reason="rejected"），v1 不允许对同一 revision 重复 request 之外的复活——重新 request 生成新 approval_id。
- **失效（invalidate，机器自动）**：
  - 工程 revision ≠ approval.engineering_revision → `stale`（reason="revision_changed"）；
  - approval active 后出现 status=open/reopened 的线程（新建或重开）→ `stale`（reason="review_reopened"）。
  - 失效是**读取期惰性判定 + 写操作前强制判定**（见 §7），不改历史记录本身。

## 3. Anchor / staleness 语义（冻结）

- anchor_revision 创建时钉死，**永不漂移**。读取时 `current_revision != anchor_revision` → 线程标记 `stale`（展示层），记录不变、不重挂。
- element_id 锚定：读取时若元素不存在 → `orphaned`；存在但工程 id 重用（同名新元素）不视为同一对象——以 element_id 精确匹配，绝不模糊重挂。
- drawing-level 线程（element_id=None）只随 revision 维度变 stale。
- stale/orphaned 是**派生展示状态**（查询时计算），不落库、不进 digest。

## 4. Actor trust model

- `actor_kind ∈ {"operator", "agent"}`。resolve/reopen/approve/reject 仅接受 `operator`；request/comment 两者皆可（comment 作者如实标记 kind）。
- v1 可信人类身份 = **本机操作者身份**：服务端启动时载入 `PID_AGENT_OPERATOR_IDENTITY`（姓名/工号字符串，本地配置），前端 review 面板的一切治理操作必须携带由服务端注入页面的 **operator token**（随机值，服务启动生成、仅本机回环接受；前端每次治理请求带 `X-Operator-Token`）。无 token 或 token 不符 → 401/403 `actor_not_trusted`。
- 服务端从 token 解出 operator 身份，**绝不接受 caller 提交的任意 actor 字符串**——直接呼应 P0-10。
- agent 调用治理 API 时必须以 agent 通道（既有 harness 审计上下文）进入，actor_kind 由服务端判定，不可自证。

## 5. review_snapshot_digest

- `review_snapshot/1`：对 ReviewState 的规范投影（threads 按 thread_id 排序、comments 按 (thread_id, entry_seq) 排序、approvals 按 approval_id 排序；字段白名单不含 created_at/actor）做 `sha256(json.dumps(sort_keys=True, separators))`。
- 用途：approval 绑定 + WS2 evidence 复用；**不属于**工程 semantic/layout digest（契约已批）。

## 6. Approval 绑定（v1 机器可验闭环）

- approve 前置校验（全部硬失败）：① operator token 有效；② `approval.engineering_revision == 当前 revision`；③ readiness_snapshot_hash == 当前 assess_release_readiness 的重算值；④ review_snapshot_digest == 当前重算值；⑤ **不存在 status ∈ {open, reopened} 的线程**（`approval_blocked_open_threads`）。
- 绑定四元组：document_id + exact engineering_revision + readiness hash + review digest（模型字段即此）。
- 跨 revision 不继承；reopen/new thread 即失效（§2）。

## 7. API surface（`/api/v2` 新路由，全部 audited）

| 方法/路径 | 行为 | 治理 revision 副作用 |
| --- | --- | --- |
| GET /documents/{id}/review | 线程+comment 列表（含派生 stale/orphaned 标记）+ 当前 review_snapshot_digest + governance_seq | 无 |
| POST /documents/{id}/review/threads | 建线程（operator/agent；anchor=当前 revision） | governance_seq+1，review snapshot 变 |
| POST /review/threads/{tid}/comments | append comment | 同上 |
| POST /review/threads/{tid}/resolve · /reopen | operator only | 同上 |
| POST /documents/{id}/approval/request | 发起（校验无 active approval） | 同上 |
| POST /approval/{aid}/decide | approve/reject，operator only，§6 全校验 | 同上 |
| GET /documents/{id}/approval | 当前/历史 approval + 失效原因 | 无 |

**关键不变量：以上全部不触碰 DocumentService.apply_transaction——engineering revision 永不因治理操作增加。** 工程修改仍只有既有单一写通道。

## 8. 最小 web review UI（WS1 验收的必要组成）

前端新增 Review 面板（v1 单页侧栏）：线程列表（按锚点元素显示、stale/orphaned 徽标）、线程内 comment 流、resolve/reopen 按钮、approval request/decide 区（显示四元组绑定与失效原因）、操作者身份横幅（"以 <operator> 身份"）。无操作者 token 时面板只读并提示。

## 9. Audit events

`review.thread.created` / `review.comment.posted` / `review.thread.resolved` / `review.thread.reopened` / `approval.requested` / `approval.decided` / `approval.invalidated`，走既有 audit 机制，metadata 含 actor_kind/actor id/thread/approval id。

## 10. Migration

store 新增 `review_state` 存储面（additive）；旧文档 = 零 review records（ReviewState 缺省空）。不改任何既有表结构 → 旧图纸 identity 零漂移。语义与契约冻结"additive/backward-compatible"一致。

## 11. Failure codes（结构化 detail.code）

`actor_not_trusted` / `review_thread_not_found` / `review_thread_stale`（对 stale 线程的 resolve/reopen——v1 允许但要求前端确认，服务端放行并记 audit；或冻结为拒绝？**默认拒绝，等 Gate 二选一**）/ `approval_blocked_open_threads` / `approval_revision_mismatch` / `approval_readiness_mismatch` / `approval_review_snapshot_mismatch` / `approval_already_active` / `element_anchor_orphaned`（仅警告不进错误）。

## 12. Backward compatibility & 测试矩阵

- 兼容：既有全部测试不动；治理 API 缺省空态；工程链（layout digest / spec digest / materialization）对 review 面零感知。
- 测试矩阵：thread 生命周期+audit；append-only（改/删尝试被拒）；anchor 不漂移（revision 前进后 stale 展示、记录不变）；orphaned 派生；operator token 缺失/错误 403；agent 不能 approve/reject；approve 五重校验逐条负测；revision 变化→approval stale；reopen→approval stale；review_snapshot_digest 确定性（乱序输入同 digest）；治理操作不增 engineering revision（GET revision 前后不变）；migration 旧库零记录打开；e2e（UI：建线程→评论→resolve→request→approve 全链）。

## 13. 预计文件白名单（实现获批后，最终以报审为准）

新增：`backend/agentcad/review_models.py`、`backend/agentcad/review_service.py`（纯治理逻辑）、`backend/agentcad/api_review.py`、store 的 review_state 面（`backend/agentcad/store.py` additive）、前端 review 面板组件 + `frontend/src/api.ts` 增量；测试：`backend/tests/test_m9_review_workflow*.py` + `frontend/e2e/review.spec.ts`。既有 ToolApproval、工程写通道、digest 体系一律不动。
