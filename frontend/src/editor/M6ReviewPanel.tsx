import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  fetchM6CandidateDetail,
  fetchM6Candidates,
  postM6Decision,
  type M6CandidateDetail,
  type M6CandidateQueue,
  type M6DecisionRequest,
} from "../api";
import { useWorkspace } from "../store";
import "./M6ReviewPanel.css";

/**
 * M6-2B-D3: the human review queue for semantic candidates.
 *
 * Everything on screen is a fact the server recorded: the proposed semantics, the evidence,
 * the decision log. The panel itself decides nothing for the reviewer — it files their
 * declared decisions. The reviewer identity is a *declared* name typed by the operator; the
 * service token (shared deployments) only authenticates the request and is never shown as
 * a person.
 */

const REVIEWER_SESSION_KEY = "pid-agent.m6-reviewer";

const TYPE_LABELS: Record<string, string> = {
  symbol_class: "设备类别",
  equipment_tag: "设备位号",
  annotation_role: "标注角色",
  connection_relationship: "连接关系",
  unresolved: "拒识",
};

const STATUS_LABELS: Record<string, string> = {
  proposed: "已提出",
  needs_review: "待审阅",
  confirmed: "已确认",
  rejected: "已拒绝",
  superseded: "已被取代",
  conflicted: "基线冲突",
  applied: "已应用",
};

function factsSummary(row: { proposed_semantics: Record<string, string> }): string {
  const facts = row.proposed_semantics;
  return (
    facts.equipment_tag ||
    facts.symbol_class ||
    (facts.annotation_role as string) ||
    (facts.unresolved_reason as string) ||
    "—"
  );
}

export function M6ReviewPanel() {
  const document = useWorkspace((state) => state.document);
  const [queue, setQueue] = useState<M6CandidateQueue | null>(null);
  const [detail, setDetail] = useState<M6CandidateDetail | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewer, setReviewer] = useState(() => {
    try {
      return window.sessionStorage.getItem(REVIEWER_SESSION_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [actionText, setActionText] = useState("");
  const [note, setNote] = useState("");
  const [reassignOpen, setReassignOpen] = useState(false);
  const [reassignRefs, setReassignRefs] = useState("");
  const [reassignTag, setReassignTag] = useState("");
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolutionText, setResolutionText] = useState("");
  const [resolutionChoice, setResolutionChoice] = useState<"keep_existing" | "accept_proposed">("keep_existing");

  const documentId = document?.id ?? "";

  const refresh = useCallback(async () => {
    if (!documentId) return;
    const next = await fetchM6Candidates(documentId);
    setQueue(next);
    if (selected) {
      try {
        setDetail(await fetchM6CandidateDetail(documentId, selected));
      } catch {
        setDetail(null);
        setSelected("");
      }
    }
  }, [documentId, selected]);

  useEffect(() => {
    refresh().catch((reason) =>
      setError(reason instanceof ApiError ? reason.message : String(reason)),
    );
  }, [refresh]);

  const openDetail = async (candidateId: string) => {
    setError("");
    setSelected(candidateId);
    setReassignOpen(false);
    setResolveOpen(false);
    try {
      setDetail(await fetchM6CandidateDetail(documentId, candidateId));
    } catch (reason) {
      setDetail(null);
      setError(reason instanceof ApiError ? reason.message : String(reason));
    }
  };

  const decide = async (payload: M6DecisionRequest) => {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      await postM6Decision(documentId, selected, payload);
      setActionText("");
      setNote("");
      setReassignOpen(false);
      setResolveOpen(false);
      setDetail(await fetchM6CandidateDetail(documentId, selected));
      setQueue(await fetchM6Candidates(documentId));
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const unresolvedSelected = detail?.candidate.proposed_semantics?.unresolved_reason != null;
  const reviewable = detail?.review_status === "needs_review";
  const conflicted = detail?.review_status === "conflicted";
  const sourceAvailable = queue?.source.available ?? false;

  const decisionLog = useMemo(
    () =>
      (detail?.decisions ?? []).map((decision) => ({
        id: String(decision.review_decision_id ?? ""),
        kind: String(decision.kind ?? ""),
        from: String(decision.from_status ?? ""),
        to: String(decision.to_status ?? ""),
        reviewer: String(decision.reviewer_identity ?? ""),
      })),
    [detail],
  );

  if (!document) {
    return <p className="report-empty">先打开一张图纸，再查看它的 M6 摄取候选。</p>;
  }

  return (
    <div className="m6-review-panel" data-testid="m6-review-panel">
      <div className="m6-review-source" data-testid="m6-source-status">
        {queue?.source.available ? (
          <span>证据源 revision {queue.source.revision}（已钉住，核验通过）</span>
        ) : (
          <span data-testid="m6-source-unavailable">
            证据源不可用{queue?.source.code ? `：${queue.source.code}` : ""}
          </span>
        )}
        <button type="button" data-testid="m6-refresh" onClick={() => refresh().catch(() => undefined)}>
          刷新
        </button>
      </div>
      <label className="m6-reviewer">
        审阅者（声明身份，非认证）
        <input
          data-testid="m6-reviewer-identity"
          aria-label="审阅者身份"
          placeholder="如 engineer.joe"
          value={reviewer}
          onChange={(event) => {
            setReviewer(event.target.value);
            try {
              window.sessionStorage.setItem(REVIEWER_SESSION_KEY, event.target.value);
            } catch {
              // sessionStorage unavailable: identity lasts for this page view only
            }
          }}
        />
      </label>
      {error ? (
        <p className="m6-review-error" role="alert" data-testid="m6-error">
          {error}
        </p>
      ) : null}
      {!queue?.candidates.length ? (
        <p className="report-empty" data-testid="m6-empty">
          该文档没有 M6 摄取候选。候选由确定性摄取产生，不在此面板创建。
        </p>
      ) : (
        <table className="m6-queue" data-testid="m6-queue">
          <thead>
            <tr>
              <th>类型</th>
              <th>内容</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {queue.candidates.map((row) => (
              <tr
                key={row.candidate_id}
                data-testid={`m6-row-${row.candidate_id}`}
                data-status={row.review_status}
                className={row.candidate_id === selected ? "selected" : ""}
                onClick={() => void openDetail(row.candidate_id)}
              >
                <td>{TYPE_LABELS[row.candidate_type] ?? row.candidate_type}</td>
                <td>{factsSummary(row)}</td>
                <td>{STATUS_LABELS[row.review_status] ?? row.review_status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {detail ? (
        <section className="m6-detail" data-testid="m6-detail">
          <h3>候选详情</h3>
          <dl className="m6-detail-facts">
            <dt>类型</dt>
            <dd>{TYPE_LABELS[detail.candidate.candidate_type as string] ?? String(detail.candidate.candidate_type)}</dd>
            <dt>状态</dt>
            <dd data-testid="m6-detail-status">{STATUS_LABELS[detail.review_status] ?? detail.review_status}</dd>
            <dt>区域</dt>
            <dd>{detail.candidate.region.region_id.slice(0, 24)}…（{detail.candidate.region.element_refs.length} 个元素）</dd>
            <dt>目标文档</dt>
            <dd>
              {detail.target.target_document_id || "（未声明）"}
              {detail.target.exists ? `（revision ${detail.target.revision}）` : "（尚不存在）"}
            </dd>
          </dl>
          <h4>事实声明</h4>
          <pre data-testid="m6-facts">{JSON.stringify(detail.candidate.proposed_semantics, null, 2)}</pre>
          <h4>证据</h4>
          <ul className="m6-evidence" data-testid="m6-evidence">
            {detail.candidate.evidence.map((item, index) => (
              <li key={index}>
                <code>{item.kind}</code> {item.detail}
              </li>
            ))}
          </ul>
          <h4>决策历史</h4>
          <ul className="m6-decisions" data-testid="m6-decisions">
            {decisionLog.map((entry) => (
              <li key={entry.id}>
                {entry.kind}: {entry.from} → {entry.to}
                {entry.reviewer ? `（${entry.reviewer}）` : ""}
              </li>
            ))}
          </ul>
          <label className="m6-reviewer">
            审阅动作（记录你做了什么）
            <input
              data-testid="m6-reviewer-action"
              aria-label="审阅动作"
              placeholder="如：核对原图位号与设备簇一致"
              value={actionText}
              onChange={(event) => setActionText(event.target.value)}
            />
          </label>
          <label className="m6-reviewer">
            备注（可选）
            <input
              data-testid="m6-note"
              aria-label="备注"
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <div className="m6-actions">
            <button
              type="button"
              data-testid="m6-confirm"
              disabled={busy || !reviewable || unresolvedSelected || !sourceAvailable}
              onClick={() =>
                void decide({
                  action: "confirm",
                  reviewer_identity: reviewer,
                  reviewer_action: actionText,
                  note,
                })
              }
            >
              确认
            </button>
            <button
              type="button"
              data-testid="m6-reject"
              disabled={busy || !reviewable || !sourceAvailable}
              onClick={() =>
                void decide({
                  action: "reject",
                  reviewer_identity: reviewer,
                  reviewer_action: actionText,
                  note,
                })
              }
            >
              拒绝
            </button>
            <button
              type="button"
              data-testid="m6-recheck"
              disabled={busy || !sourceAvailable}
              onClick={() =>
                void decide({ action: "recheck", reviewer_identity: reviewer, note })
              }
            >
              复核基线
            </button>
            <button
              type="button"
              data-testid="m6-reassign"
              disabled={busy || !sourceAvailable}
              onClick={() => {
                setReassignOpen((open) => !open);
                setReassignRefs(detail.candidate.region.element_refs.join(", "));
                setReassignTag(
                  String(detail.candidate.proposed_semantics.equipment_tag ?? ""),
                );
              }}
            >
              改派关联
            </button>
            {conflicted ? (
              <button
                type="button"
                data-testid="m6-resolve"
                disabled={busy || !sourceAvailable}
                onClick={() => setResolveOpen((open) => !open)}
              >
                解除冲突
              </button>
            ) : null}
          </div>
          {reassignOpen ? (
            <div className="m6-reassign" data-testid="m6-reassign-form">
              <p>改派 = 登记一条更正候选（新证据集），并把旧候选标记为被它取代；历史不改写。</p>
              <label className="m6-reviewer">
                更正后的证据元素集（逗号分隔的元素 id）
                <textarea
                  data-testid="m6-reassign-refs"
                  aria-label="更正后的证据元素集"
                  rows={3}
                  value={reassignRefs}
                  onChange={(event) => setReassignRefs(event.target.value)}
                />
              </label>
              <label className="m6-reviewer">
                位号（旧候选无位号时必填）
                <input
                  data-testid="m6-reassign-tag"
                  aria-label="改派位号"
                  value={reassignTag}
                  onChange={(event) => setReassignTag(event.target.value)}
                />
              </label>
              <button
                type="button"
                data-testid="m6-reassign-submit"
                disabled={busy}
                onClick={() =>
                  void decide({
                    action: "reassign",
                    reviewer_identity: reviewer,
                    reviewer_action: actionText || "改派关联",
                    note,
                    element_refs: reassignRefs
                      .split(",")
                      .map((ref) => ref.trim())
                      .filter(Boolean),
                    equipment_tag: reassignTag,
                  })
                }
              >
                提交改派
              </button>
            </div>
          ) : null}
          {resolveOpen && conflicted ? (
            <div className="m6-resolve" data-testid="m6-resolve-form">
              <label className="m6-reviewer">
                冲突解除说明
                <textarea
                  data-testid="m6-resolution"
                  aria-label="冲突解除说明"
                  rows={3}
                  value={resolutionText}
                  onChange={(event) => setResolutionText(event.target.value)}
                />
              </label>
              <label className="m6-reviewer">
                解除方式
                <select
                  data-testid="m6-resolution-choice"
                  aria-label="解除方式"
                  value={resolutionChoice}
                  onChange={(event) =>
                    setResolutionChoice(event.target.value as "keep_existing" | "accept_proposed")
                  }
                >
                  <option value="keep_existing">保留既有值</option>
                  <option value="accept_proposed">接受提案值</option>
                </select>
              </label>
              <button
                type="button"
                data-testid="m6-resolve-submit"
                disabled={busy}
                onClick={() =>
                  void decide({
                    action: "resolve_conflict",
                    reviewer_identity: reviewer,
                    reviewer_action: actionText || "复核基线冲突并解除",
                    note,
                    conflict_resolution: resolutionText,
                    resolution_choice: resolutionChoice,
                  })
                }
              >
                提交解除
              </button>
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
