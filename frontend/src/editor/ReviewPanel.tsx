import { useCallback, useEffect, useState } from "react";

import { useWorkspace } from "../store";

type ReviewThreadView = {
  thread_id: string;
  anchor_revision: number;
  element_id: string | null;
  subject_text: string;
  status: "open" | "resolved" | "reopened";
  created_by: string;
  created_by_kind: "operator" | "agent";
  resolution_note: string;
  derived_stale: boolean;
  derived_orphaned: boolean;
};

type ReviewCommentView = {
  comment_id: string;
  thread_id: string;
  entry_seq: number;
  body: string;
  author: string;
  author_kind: "operator" | "agent";
  created_at: string;
};

type ApprovalView = {
  approval_id: string;
  engineering_revision: number;
  status: string;
  decision: string | null;
  requested_by: string;
  decided_by: string | null;
  decision_readiness_hash: string;
  invalidation_reason: string | null;
  derived_liveness: string;
};

type ReviewView = {
  document_id: string;
  governance_seq: number;
  review_snapshot_digest: string;
  operator_enabled: boolean;
  threads: ReviewThreadView[];
  comments: ReviewCommentView[];
  approvals: ApprovalView[];
};

async function reviewFetch<T>(path: string, init: RequestInit = {}, token = ""): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { "X-Operator-Token": token } : {}),
  };
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { code?: string; message?: string } | string;
    };
    const detail = body.detail;
    const message =
      typeof detail === "string" ? detail : (detail?.message ?? `HTTP ${response.status}`);
    const code = typeof detail === "string" ? "error" : (detail?.code ?? "error");
    throw new Error(`${code}: ${message}`);
  }
  return (await response.json()) as T;
}

export function ReviewPanel() {
  const document = useWorkspace((state) => state.document);
  const [view, setView] = useState<ReviewView | null>(null);
  const [identity, setIdentity] = useState<{ token: string; name: string } | null>(null);
  const [error, setError] = useState("");
  const [newBody, setNewBody] = useState("");
  const [commentDrafts, setCommentDrafts] = useState<Record<string, string>>({});
  const [noteDrafts, setNoteDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const documentId = document?.id ?? "";

  const refresh = useCallback(async () => {
    if (!documentId) return;
    const next = await reviewFetch<ReviewView>(`/api/v2/documents/${documentId}/review`);
    setView(next);
  }, [documentId]);

  useEffect(() => {
    refresh().catch((exc) => setError(String(exc)));
  }, [refresh]);

  const becomeOperator = async () => {
    setBusy(true);
    setError("");
    try {
      const session = await reviewFetch<{ operator_token: string; identity: string }>(
        "/api/v2/review/operator-session",
        { method: "POST" },
      );
      setIdentity({ token: session.operator_token, name: session.identity });
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  if (!documentId) {
    return <p className="review-empty">打开一张图纸后可使用审查工作流。</p>;
  }
  if (!view) {
    return <p className="review-empty">载入审查状态…</p>;
  }

  const seq = view.governance_seq;
  const token = identity?.token ?? "";

  return (
    <div className="review-panel">
      <div className="review-identity" data-testid="review-identity">
        {identity ? (
          <span>
            以操作者身份：<strong>{identity.name}</strong>（人审操作已解锁）
          </span>
        ) : (
          <span>当前为 agent 身份（只读 + 可发评论）</span>
        )}
        {!identity && view.operator_enabled ? (
          <button type="button" onClick={becomeOperator} disabled={busy}>
            解锁人审操作
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="review-error" role="alert">
          {error}
        </p>
      ) : null}

      <section className="review-threads">
        <h3>审查线程</h3>
        {view.threads.length === 0 ? <p className="review-empty">暂无审查意见。</p> : null}
        {view.threads.map((thread) => (
          <article key={thread.thread_id} className={`review-thread review-thread--${thread.status}`}>
            <header>
              <strong>{thread.subject_text || "图纸级评论"}</strong>
              <span className={`review-badge review-badge--${thread.status}`}>{thread.status}</span>
              {thread.derived_stale ? <span className="review-badge review-badge--stale">stale（锚定 r{thread.anchor_revision}）</span> : null}
              {thread.derived_orphaned ? <span className="review-badge review-badge--orphaned">orphaned</span> : null}
              <span className="review-meta">
                由 {thread.created_by}（{thread.created_by_kind}）发起
              </span>
            </header>
            <ul className="review-comments">
              {view.comments
                .filter((comment) => comment.thread_id === thread.thread_id)
                .map((comment) => (
                  <li key={comment.comment_id}>
                    <span className="review-meta">
                      #{comment.entry_seq} {comment.author}（{comment.author_kind}）
                    </span>
                    <p>{comment.body}</p>
                  </li>
                ))}
            </ul>
            <div className="review-actions">
              <input
                aria-label={`回复 ${thread.thread_id}`}
                placeholder="回复…"
                value={commentDrafts[thread.thread_id] ?? ""}
                onChange={(event) =>
                  setCommentDrafts((drafts) => ({ ...drafts, [thread.thread_id]: event.target.value }))
                }
              />
              <button
                type="button"
                disabled={busy || !(commentDrafts[thread.thread_id] ?? "").trim()}
                onClick={() =>
                  run(async () => {
                    await reviewFetch(
                      `/api/v2/documents/${documentId}/review/threads/${thread.thread_id}/comments`,
                      {
                        method: "POST",
                        body: JSON.stringify({
                          body: commentDrafts[thread.thread_id].trim(),
                          expected_governance_seq: seq,
                        }),
                      },
                      token,
                    );
                    setCommentDrafts((drafts) => ({ ...drafts, [thread.thread_id]: "" }));
                  })
                }
              >
                回复
              </button>
              {thread.status === "resolved" ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    run(() =>
                      reviewFetch(
                        `/api/v2/documents/${documentId}/review/threads/${thread.thread_id}/reopen`,
                        { method: "POST", body: JSON.stringify({ expected_governance_seq: seq }) },
                        token,
                      ),
                    )
                  }
                >
                  重开
                </button>
              ) : (
                <>
                  <input
                    aria-label={`闭环说明 ${thread.thread_id}`}
                    placeholder="闭环说明（必填）"
                    value={noteDrafts[thread.thread_id] ?? ""}
                    onChange={(event) =>
                      setNoteDrafts((drafts) => ({ ...drafts, [thread.thread_id]: event.target.value }))
                    }
                  />
                  <button
                    type="button"
                    disabled={busy || !(noteDrafts[thread.thread_id] ?? "").trim()}
                    onClick={() =>
                      run(async () => {
                        await reviewFetch(
                          `/api/v2/documents/${documentId}/review/threads/${thread.thread_id}/resolve`,
                          {
                            method: "POST",
                            body: JSON.stringify({
                              resolution_note: noteDrafts[thread.thread_id].trim(),
                              expected_governance_seq: seq,
                            }),
                          },
                          token,
                        );
                        setNoteDrafts((drafts) => ({ ...drafts, [thread.thread_id]: "" }));
                      })
                    }
                  >
                    闭环
                  </button>
                </>
              )}
            </div>
          </article>
        ))}
        <div className="review-new">
          <input
            aria-label="新审查意见"
            placeholder="提出审查意见（图纸级）…"
            value={newBody}
            onChange={(event) => setNewBody(event.target.value)}
          />
          <button
            type="button"
            disabled={busy || !newBody.trim()}
            onClick={() =>
              run(async () => {
                await reviewFetch(
                  `/api/v2/documents/${documentId}/review/threads`,
                  {
                    method: "POST",
                    body: JSON.stringify({
                      body: newBody.trim(),
                      expected_governance_seq: seq,
                    }),
                  },
                  token,
                );
                setNewBody("");
              })
            }
          >
            发起线程
          </button>
        </div>
      </section>

      <section className="review-approvals">
        <h3>工程批准</h3>
        {view.approvals.length === 0 ? <p className="review-empty">尚无批准记录。</p> : null}
        {view.approvals.map((approval) => (
          <article key={approval.approval_id} className="review-approval">
            <header>
              <strong>{approval.approval_id}</strong>
              <span className={`review-badge review-badge--${approval.derived_liveness}`}>
                {approval.status} / {approval.derived_liveness}
              </span>
            </header>
            <p className="review-meta">
              绑定 revision {approval.engineering_revision}；决策时 readiness{" "}
              {approval.decision_readiness_hash ? approval.decision_readiness_hash.slice(0, 12) : "—"}
              {approval.invalidation_reason ? `；失效原因 ${approval.invalidation_reason}` : ""}
            </p>
            {approval.status === "requested" && identity ? (
              <div className="review-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    run(() =>
                      reviewFetch(
                        `/api/v2/documents/${documentId}/approval/${approval.approval_id}/decide`,
                        {
                          method: "POST",
                          body: JSON.stringify({ decision: "approved", expected_governance_seq: seq }),
                        },
                        token,
                      ),
                    )
                  }
                >
                  批准
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    run(() =>
                      reviewFetch(
                        `/api/v2/documents/${documentId}/approval/${approval.approval_id}/decide`,
                        {
                          method: "POST",
                          body: JSON.stringify({ decision: "rejected", expected_governance_seq: seq }),
                        },
                        token,
                      ),
                    )
                  }
                >
                  拒绝
                </button>
              </div>
            ) : null}
          </article>
        ))}
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            run(() =>
              reviewFetch(
                `/api/v2/documents/${documentId}/approval/request`,
                { method: "POST", body: JSON.stringify({ expected_governance_seq: seq }) },
                token,
              ),
            )
          }
        >
          申请工程批准
        </button>
      </section>
    </div>
  );
}
