import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import {
  buildDraftingOptions,
  crossingsSummary,
  gateReasons,
  gateSummary,
  gateVerdict,
  groupFindings,
  isLockedElement,
  junctionsSummary,
  lockLabel,
  lockOperations,
  lockSummary,
  metricRows,
  metricValue,
  previewAppliesTo,
  previewScopeLabel,
  previewSummary,
  reportSummary,
  reproducibilitySummary,
  ruleSourceLabel,
  severityLabel,
  scopeKindLabel,
  stalePreviewMessage,
  summariseElementIds,
} from "../drafting";
import type { DraftingOptions, DraftingPreview, DraftingReport } from "../draftingTypes";
import { useWorkspace } from "../store";

/**
 * Deterministic drafting panel (Charter M3).
 *
 * Three deliberate properties:
 *
 * 1. **Preview first.** 预览整理 returns a transaction; nothing is written until the
 *    engineer confirms it. The panel refuses to apply a preview computed from a revision
 *    the document has since left.
 * 2. **Locks are document data.** 锁定/解除锁定 writes `metadata.drafting_lock` through an
 *    ordinary governed transaction, so a manual pin is audited, undoable and travels with
 *    the file instead of living in this panel's state.
 * 3. **The gate is explained, not just coloured.** Drafting blockers, drawing-rule errors
 *    and the score target are listed separately, because they have different fixes.
 */
export function DraftingPanel() {
  const document = useWorkspace((state) => state.document);
  const selectedElementIds = useWorkspace((state) => state.selectedElementIds);
  const setSelection = useWorkspace((state) => state.setSelection);
  const transact = useWorkspace((state) => state.transact);
  const isMutating = useWorkspace((state) => state.isMutating);

  const [scope, setScope] = useState<"document" | "selection">("document");
  const [direction, setDirection] = useState<"horizontal" | "vertical">("horizontal");
  const [targetScore, setTargetScore] = useState(95);
  const [relayout, setRelayout] = useState(true);
  const [rerouteConnectors, setRerouteConnectors] = useState(true);
  const [placeAnnotations, setPlaceAnnotations] = useState(true);
  const [bridgeCrossings, setBridgeCrossings] = useState(true);
  const [resolveCollisions, setResolveCollisions] = useState(true);
  const [waivedCodes, setWaivedCodes] = useState<string[]>([]);
  const [report, setReport] = useState<DraftingReport | null>(null);
  const [preview, setPreview] = useState<DraftingPreview | null>(null);
  const [analysing, setAnalysing] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");

  const documentId = document?.id;
  const revision = document?.revision ?? 0;

  const options = useMemo<DraftingOptions | null>(() => {
    if (!document) return null;
    return buildDraftingOptions({
      revision: document.revision,
      scope,
      selectedElementIds,
      direction,
      targetScore,
      relayout,
      rerouteConnectors,
      placeAnnotations,
      bridgeCrossings,
      resolveCollisions,
      waivedCodes,
    });
  }, [
    document,
    scope,
    selectedElementIds,
    direction,
    targetScore,
    relayout,
    rerouteConnectors,
    placeAnnotations,
    bridgeCrossings,
    resolveCollisions,
    waivedCodes,
  ]);

  useEffect(() => {
    setReport(null);
    setPreview(null);
    setError("");
  }, [documentId]);

  const lockedIds = useMemo(() => {
    if (!document) return [];
    return document.elements.filter((element) => isLockedElement(element)).map((element) => element.id);
  }, [document]);

  const selectedIds = useMemo(
    () => selectedElementIds.filter((elementId) =>
      (document?.elements ?? []).some((element) => element.id === elementId)),
    [selectedElementIds, document],
  );

  const groups = useMemo(() => groupFindings(report?.findings ?? []), [report]);
  const collisionGroups = useMemo(() => groupFindings(report?.collisions ?? []), [report]);
  const previewGroups = useMemo(() => groupFindings(preview?.findings ?? []), [preview]);
  const rows = useMemo(() => (preview ? metricRows(preview.metrics) : []), [preview]);

  if (!document) return <p className="inspector-hint">打开文档后可运行确定性整理。</p>;

  const runReport = async () => {
    if (!options) return;
    setAnalysing(true);
    setError("");
    try {
      setReport(await api.getDraftingReport(document.id, options));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setAnalysing(false);
    }
  };

  const runPreview = async () => {
    if (!options) return;
    setPreviewing(true);
    setError("");
    try {
      const next = await api.previewDrafting(document.id, options);
      setPreview(next);
      const touched = [
        ...next.moved_element_ids,
        ...next.rerouted_connector_ids,
        ...next.moved_annotation_ids,
      ];
      if (touched.length) setSelection(touched, { revealProperties: false });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setPreviewing(false);
    }
  };

  const applyPreview = async () => {
    if (!preview?.transaction) return;
    if (!previewAppliesTo(preview, document.revision)) {
      setError(stalePreviewMessage(preview, document.revision));
      return;
    }
    setApplying(true);
    setError("");
    try {
      await transact(
        preview.transaction.operations,
        preview.transaction.label || "Deterministic drafting",
      );
      setPreview(null);
      setReport(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setApplying(false);
    }
  };

  const setLock = async (locked: boolean) => {
    const targets = locked ? selectedIds : lockedIds;
    if (!targets.length) {
      setError(locked ? "请先在画布上选择要锁定的元素。" : "当前没有已锁定的元素。");
      return;
    }
    setError("");
    try {
      await transact(lockOperations(targets, locked), locked ? "Lock drafting selection" : "Unlock drafting elements");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  };

  const toggleWaiver = (code: string) => {
    setWaivedCodes((current) =>
      current.includes(code) ? current.filter((item) => item !== code) : [...current, code],
    );
  };

  const waivedNotice = waivedCodes.length ? (
    <p className="drafting-waiver" data-testid="drafting-waiver">
      已豁免：{waivedCodes.join("、")} —— 豁免只关闭门禁，不隐藏证据：相关 finding 仍会列出并标注“已豁免”。
    </p>
  ) : null;

  const findingList = (
    items: ReturnType<typeof groupFindings>,
    testId: string,
  ) => (
    <div className="drafting-findings" data-testid={testId}>
      {items.map((group) => (
        <article
          key={group.code}
          className={`drafting-row severity-${group.severity}${group.waived ? " waived" : ""}`}
          data-testid={`${testId}-${group.code}`}
        >
          <div className="drafting-row-heading">
            <strong>{group.code}</strong>
            <em>{severityLabel(group.severity)}</em>
            <span>{ruleSourceLabel(group.ruleSource)}</span>
            {group.count > 1 ? <span>×{group.count}</span> : null}
            {group.waived ? <span data-testid={`${testId}-waived-${group.code}`}>已豁免</span> : null}
            <button
              type="button"
              disabled={!group.elementIds.length}
              onClick={() => setSelection(group.elementIds, { revealProperties: false })}
            >
              定位
            </button>
            <button
              type="button"
              data-testid={`${testId}-waive-${group.code}`}
              onClick={() => toggleWaiver(group.code)}
            >
              {waivedCodes.includes(group.code) ? "取消豁免" : "豁免"}
            </button>
          </div>
          <p>{group.messages[0]}</p>
          {group.count > 1 ? <p className="drafting-hint">还有 {group.count - 1} 处同类问题</p> : null}
          <code>{summariseElementIds(group.elementIds)}</code>
        </article>
      ))}
      {items.length === 0 ? <p className="inspector-hint">没有该类问题。</p> : null}
    </div>
  );

  return (
    <div className="drafting-panel" data-testid="drafting-panel">
      <section className="drafting-settings">
        <label>整理范围
          <select
            data-testid="drafting-scope"
            value={scope}
            onChange={(event) => setScope(event.target.value as "document" | "selection")}
          >
            <option value="document">整张图</option>
            <option value="selection" disabled={!selectedIds.length}>当前选择</option>
          </select>
        </label>
        <label>主流程方向
          <select
            data-testid="drafting-direction"
            value={direction}
            onChange={(event) => setDirection(event.target.value as "horizontal" | "vertical")}
          >
            <option value="horizontal">从左到右</option>
            <option value="vertical">从上到下</option>
          </select>
        </label>
        <label>门禁目标分
          <input
            data-testid="drafting-target-score"
            type="number"
            min={0}
            max={100}
            value={targetScore}
            onChange={(event) => {
              const value = Number(event.target.value);
              setTargetScore(Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 95);
            }}
          />
        </label>
        <label className="drafting-checkbox">
          <input type="checkbox" checked={relayout} onChange={(event) => setRelayout(event.target.checked)} />
          区域重排设备（关闭则只整理走线/标注）
        </label>
        <label className="drafting-checkbox">
          <input type="checkbox" checked={rerouteConnectors} onChange={(event) => setRerouteConnectors(event.target.checked)} />
          端口感知重排管线
        </label>
        <label className="drafting-checkbox">
          <input type="checkbox" checked={placeAnnotations} onChange={(event) => setPlaceAnnotations(event.target.checked)} />
          放置标注、避让
        </label>
        <label className="drafting-checkbox">
          <input type="checkbox" checked={bridgeCrossings} onChange={(event) => setBridgeCrossings(event.target.checked)} />
          交叉加跨线桥（只画在次管线上）
        </label>
        <label className="drafting-checkbox">
          <input type="checkbox" checked={resolveCollisions} onChange={(event) => setResolveCollisions(event.target.checked)} />
          分离重叠节点
        </label>
        {waivedNotice}
        <div className="drafting-actions">
          <button type="button" data-testid="drafting-analyse" disabled={analysing || previewing || isMutating} onClick={() => void runReport()}>
            {analysing ? "检查中…" : "检查图纸"}
          </button>
          <button className="primary" type="button" data-testid="drafting-preview" disabled={analysing || previewing || isMutating} onClick={() => void runPreview()}>
            {previewing ? "正在整理…" : "预览整理"}
          </button>
        </div>
        <div className="drafting-actions">
          <button type="button" data-testid="drafting-lock" disabled={isMutating || !selectedIds.length} onClick={() => void setLock(true)}>
            {lockLabel(true)}
          </button>
          <button type="button" data-testid="drafting-unlock" disabled={isMutating || !lockedIds.length} onClick={() => void setLock(false)}>
            {lockLabel(false)}
          </button>
          <span className="drafting-hint" data-testid="drafting-locked-count">已锁定 {lockedIds.length}</span>
        </div>
        <p className="drafting-hint" data-testid="drafting-lock-summary">
          {report ? lockSummary(report.locks) : "锁定通过普通事务写入文档，可审计、可撤销。"}
        </p>
        <p className="drafting-hint">确定性整理先生成事务预览；只有确认后才会通过受治理的写通道提交。</p>
      </section>

      {error ? <div className="error-box" data-testid="drafting-error">{error}</div> : null}

      {report ? (
        <section className="drafting-report" data-testid="drafting-report">
          <div className="drafting-heading">
            <strong>图纸检查</strong>
            <span data-testid="drafting-scope-kind">{scopeKindLabel(report.scope_kind)}</span>
          </div>
          <p data-testid="drafting-report-summary">{reportSummary(report)}</p>
          <p className={`drafting-gate ${gateVerdict(report.gate).tone}`} data-testid="drafting-gate">
            {gateSummary(report.gate)}
          </p>
          {gateReasons(report.gate).length ? (
            <ul className="drafting-reasons" data-testid="drafting-gate-reasons">
              {gateReasons(report.gate).map((reason) => <li key={reason} data-testid="drafting-gate-reason">{reason}</li>)}
            </ul>
          ) : null}
          <p data-testid="drafting-crossings">{crossingsSummary(report.crossings)}</p>
          <p data-testid="drafting-junctions">{junctionsSummary(report.junctions)}</p>
          <p>可寻址端口 {report.ports.length}</p>
          {findingList(groups, "drafting-report-findings")}
          {collisionGroups.length ? (
            <>
              <h3>重叠与碰撞</h3>
              {findingList(collisionGroups, "drafting-collisions")}
            </>
          ) : null}
        </section>
      ) : null}

      {preview ? (
        <section className="drafting-preview" data-testid="drafting-preview-result">
          <div className="drafting-heading">
            <strong>整理预览</strong>
            <span data-testid="drafting-preview-scope">{previewScopeLabel(preview)}</span>
          </div>
          <p data-testid="drafting-preview-summary">{previewSummary(preview)}</p>
          <p className={`drafting-gate ${gateVerdict(preview.gate).tone}`} data-testid="drafting-preview-gate">
            {gateSummary(preview.gate)}
          </p>
          <p data-testid="drafting-preview-digest">{reproducibilitySummary(preview)}</p>
          <dl className="drafting-metrics" data-testid="drafting-metrics">
            {rows.map((row) => (
              <div key={row.key} className={row.improved ? "improved" : row.worsened ? "worsened" : ""}>
                <dt>{row.label}</dt>
                <dd data-testid={`drafting-metric-${row.key}`}>
                  {metricValue(row.before, row.digits)} → <strong>{metricValue(row.after, row.digits)}</strong>
                </dd>
              </div>
            ))}
          </dl>
          {preview.metrics.regressions.length ? (
            <div className="error-box" data-testid="drafting-regressions">
              <strong>整理结果使图面变差</strong>
              <ul>{preview.metrics.regressions.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          ) : null}
          {preview.metrics.improvements.length ? (
            <ul className="drafting-improvements" data-testid="drafting-improvements">
              {preview.metrics.improvements.map((item) => <li key={item}>{item}</li>)}
            </ul>
          ) : null}
          {preview.warnings.length ? (
            <ul className="drafting-warnings" data-testid="drafting-warnings">
              {preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          ) : null}
          {preview.skipped_locked_element_ids.length ? (
            <p className="drafting-hint" data-testid="drafting-skipped-locked">
              跳过锁定元素：{summariseElementIds(preview.skipped_locked_element_ids)}
            </p>
          ) : null}
          {findingList(previewGroups, "drafting-preview-findings")}
          <div className="drafting-actions">
            <button className="confirm" type="button" data-testid="drafting-apply" disabled={applying || isMutating || !preview.transaction} onClick={() => void applyPreview()}>
              {applying ? "正在应用…" : "确认应用整理"}
            </button>
            <button type="button" data-testid="drafting-discard" disabled={applying} onClick={() => { setPreview(null); setError(""); }}>
              放弃预览
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}
