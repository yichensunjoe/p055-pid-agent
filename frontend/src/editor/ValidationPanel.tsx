import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { useWorkspace } from "../store";
import {
  approvalDisclaimer,
  bindEvaluation,
  bindingNotice,
  filterIssues,
  issueDetailRows,
  severityLabels,
  severityOrder,
  sortIssues,
  type EvaluationBinding,
  type IssueFilter,
  type ReleaseReadiness,
  type ValidationResult,
} from "../validation";

/**
 * The canonical validation result, read-only (M4, Charter §49).
 *
 * Everything on screen comes from the server's `ValidationResult`; the panel sorts and
 * filters for reading, and never decides anything. It deliberately shows *waived* issues
 * too, because a waiver annotates a finding rather than removing it, and it shows the
 * evaluation time, the rule-bundle fingerprint and the skipped validators, because those
 * are the facts that make the verdict reproducible.
 */
export function ValidationPanel() {
  const document = useWorkspace((state) => state.document);
  const setSelection = useWorkspace((state) => state.setSelection);
  const [asOf, setAsOf] = useState("");
  const [usedMoment, setUsedMoment] = useState("");
  const [filter, setFilter] = useState("");
  const [severity, setSeverity] = useState<IssueFilter>("all");
  const [result, setResult] = useState<ValidationResult | null>(null);
  const [readiness, setReadiness] = useState<ReleaseReadiness | null>(null);
  const [binding, setBinding] = useState<EvaluationBinding | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!document) {
      setResult(null);
      setReadiness(null);
      setBinding(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    // One evaluation instant per refresh, sent explicitly to *both* requests. Leaving it
    // empty would let the server pick two different `now` values, and readiness would
    // then describe a validation result the reviewer is not looking at (R3 P0-5).
    const moment = asOf.trim() || new Date().toISOString();
    setUsedMoment(moment);
    void Promise.all([
      api.getValidation(document.id, moment),
      api.getReleaseReadiness(document.id, moment),
    ])
      .then(([nextResult, nextReadiness]) => {
        if (cancelled) return;
        setResult(nextResult);
        setReadiness(nextReadiness);
        setBinding(bindEvaluation(nextResult, nextReadiness));
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof ApiError ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [document?.id, document?.revision, asOf, nonce]);

  const issues = useMemo(
    () => (result ? filterIssues(sortIssues(result.issues), filter, severity) : []),
    [result, filter, severity],
  );

  const locate = (elementIds: string[]) => {
    if (!document) return;
    const available = new Set(document.elements.map((element) => element.id));
    const target = elementIds.filter((id) => available.has(id));
    if (target.length) setSelection(target);
  };

  if (!document) return <p className="report-empty">先打开一张图纸，再运行工程校验。</p>;

  return (
    <div className="validation-panel" data-testid="validation-panel">
      <div className="report-controls">
        <label>
          评估时刻
          <input
            aria-label="校验评估时刻"
            onChange={(event) => setAsOf(event.target.value)}
            placeholder="留空则用当前时间"
            value={asOf}
          />
        </label>
        <button onClick={() => setNonce((value) => value + 1)} type="button">
          重新校验
        </button>
        {usedMoment ? (
          <span data-testid="validation-as-of" title="两次请求使用的同一个评估时刻">
            本次评估时刻 {usedMoment}
          </span>
        ) : null}
      </div>

      {binding ? (
        <p
          className={binding.bound ? "validation-binding" : "validation-binding report-error"}
          data-mismatches={binding.mismatches.join(",")}
          data-state={binding.bound ? "bound" : "mismatched"}
          data-testid="validation-binding"
        >
          {bindingNotice(binding)}
          {result && binding.bound ? ` 结果哈希 ${result.result_hash.slice(0, 12)}…` : ""}
        </p>
      ) : null}

      {readiness && binding?.bound ? (
        <p className="validation-state" data-testid="validation-readiness" data-state={readiness.state}>
          发布就绪：{readiness.state === "eligible" ? "eligible（可申请）" : "not_eligible（不可申请）"}
          {readiness.reasons.length ? ` · ${readiness.reasons.join("；")}` : ""}
        </p>
      ) : null}

      {result ? (
        <dl className="validation-provenance" data-testid="validation-provenance">
          <div><dt>文档 / 版本</dt><dd>{result.document_id}@{result.revision}</dd></div>
          <div><dt>文档哈希</dt><dd>{result.content_hash.slice(0, 12)}…</dd></div>
          <div><dt>规则包指纹</dt><dd>{result.rule_bundle_fingerprint.slice(0, 12)}…</dd></div>
          <div><dt>profile</dt><dd>{result.profile_id}@{result.profile_version}</dd></div>
          <div><dt>引擎</dt><dd>{result.engine_version}</dd></div>
          <div><dt>符号目录指纹</dt><dd>{result.symbol_registry_fingerprint.slice(0, 12)}…</dd></div>
          <div><dt>评估时刻</dt><dd>{result.evaluated_at}</dd></div>
          <div><dt>结果哈希</dt><dd>{result.result_hash.slice(0, 12)}…</dd></div>
          <div>
            <dt>计数</dt>
            <dd>
              阻断 {result.counts.blocker} · 错误 {result.counts.error} · 警告 {result.counts.warning} ·
              提示 {result.counts.info} · 已豁免 {result.counts.waived} · 未注册 {result.counts.unregistered}
            </dd>
          </div>
          <div><dt>已运行校验器</dt><dd>{result.validators_run.join(", ")}</dd></div>
          {result.validators_skipped.length ? (
            <div>
              <dt>未运行（不等同通过）</dt>
              <dd>
                {result.validators_skipped
                  .map((skip) => `${skip.validator_id}（${skip.code}）`)
                  .join(", ")}
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="report-controls">
        <label>
          搜索
          <input
            aria-label="校验问题搜索"
            onChange={(event) => setFilter(event.target.value)}
            value={filter}
          />
        </label>
        <label>
          级别
          <select
            aria-label="校验级别过滤"
            onChange={(event) => setSeverity(event.target.value as IssueFilter)}
            value={severity}
          >
            <option value="all">全部</option>
            <option value="waived">已豁免</option>
            {severityOrder.map((item) => (
              <option key={item} value={item}>{severityLabels[item]}</option>
            ))}
          </select>
        </label>
      </div>

      {loading ? <p className="report-empty">正在校验…</p> : null}
      {error ? <p className="report-error" role="alert">{error}</p> : null}
      {!loading && result && issues.length === 0 ? (
        <p className="report-empty">当前过滤条件下没有校验问题。</p>
      ) : null}

      <ul className="validation-issues" data-testid="validation-issues">
        {issues.map((issue) => (
          <li
            className="validation-issue"
            data-severity={issue.severity}
            data-testid="validation-issue"
            key={`${issue.code}-${issue.waiver_status}-${issue.element_ids.join("-")}`}
          >
            <button
              className="validation-issue-title"
              onClick={() => locate(issue.element_ids)}
              type="button"
            >
              {issue.code} · {severityLabels[issue.severity]}
              {issue.waiver_status !== "not_waived" ? ` · ${issue.waiver_status}` : ""}
            </button>
            <p className="validation-issue-message">{issue.message}</p>
            <dl>
              {issueDetailRows(issue).map((row) => (
                <div key={row.label}>
                  <dt>{row.label}</dt>
                  <dd>{row.value}</dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>

      <p className="validation-disclaimer" data-testid="validation-disclaimer">
        {approvalDisclaimer()}
      </p>
    </div>
  );
}

export const validationPanelTestIds = {
  panel: "validation-panel",
  binding: "validation-binding",
  readiness: "validation-readiness",
  provenance: "validation-provenance",
  issues: "validation-issues",
  issue: "validation-issue",
  disclaimer: "validation-disclaimer",
};

export const fetchValidationResult = (
  ...args: Parameters<typeof api.getValidation>
): ReturnType<typeof api.getValidation> => api.getValidation(...args);
