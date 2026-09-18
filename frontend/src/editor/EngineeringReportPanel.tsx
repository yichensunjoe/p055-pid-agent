import { useEffect, useMemo, useState } from "react";
import { api, ApiError, authorizedFetch } from "../api";
import {
  filterReportRows,
  reportRowElementIds,
  reportTabCount,
  reportTabLabels,
  type ReportRow,
  type ReportTab,
} from "../engineeringReports";
import { useWorkspace } from "../store";
import type { EngineeringReport, ReportScope, RuleFinding } from "../types";

const visibleReportTabs: ReportTab[] = ["equipment", "lines", "instruments"];

function isFinding(row: ReportRow): row is RuleFinding {
  return "element_ids" in row;
}

function rowKey(row: ReportRow): string {
  if (isFinding(row)) return `${row.severity}-${row.code}-${row.element_ids.join("-")}`;
  return row.element_id;
}

function rowTitle(row: ReportRow): string {
  if (isFinding(row)) return `${row.code} · ${row.severity}`;
  if ("line_tag" in row) return row.line_tag || row.element_id;
  return row.tag || row.element_id;
}

function rowSummary(row: ReportRow): string {
  if (isFinding(row)) return row.message;
  if ("line_tag" in row) {
    return [row.medium, row.nominal_diameter, row.source, "→", row.target].filter(Boolean).join(" · ");
  }
  return [row.symbol_name, row.category, row.layer_name, row.system_name].filter(Boolean).join(" · ");
}

export function EngineeringReportPanel() {
  const document = useWorkspace((state) => state.document);
  const setSelection = useWorkspace((state) => state.setSelection);
  const [scope, setScope] = useState<ReportScope>("visible");
  const [tab, setTab] = useState<ReportTab>("equipment");
  const [filter, setFilter] = useState("");
  const [report, setReport] = useState<EngineeringReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    if (!document) {
      setReport(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    void api.getEngineeringReport(document.id, scope)
      .then((nextReport) => {
        if (!cancelled) setReport(nextReport);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof ApiError ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [document?.id, document?.revision, scope, reloadNonce]);

  const rows = useMemo(
    () => report ? filterReportRows(report, tab, filter) : [],
    [report, tab, filter],
  );

  const locate = (row: ReportRow) => {
    if (!document) return;
    const available = new Set(document.elements.map((element) => element.id));
    const elementIds = reportRowElementIds(row).filter((elementId) => available.has(elementId));
    if (elementIds.length) setSelection(elementIds, { revealProperties: false });
  };

  const downloadCsv = async () => {
    if (!document) return;
    setDownloading(true);
    setError("");
    try {
      const response = await authorizedFetch(api.engineeringReportCsvUrl(document.id, tab, scope));
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = `${document.id}-${scope}-${tab}.csv`;
      window.document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(`CSV 下载失败：${reason instanceof Error ? reason.message : String(reason)}`);
    } finally {
      setDownloading(false);
    }
  };

  if (!document) return <p className="inspector-hint">打开文档后可生成工程报表。</p>;

  return (
    <div className="engineering-report-panel" data-testid="engineering-report-panel">
      <div className="report-toolbar">
        <label>范围
          <select data-testid="report-scope" value={scope} onChange={(event) => setScope(event.target.value as ReportScope)}>
            <option value="visible">可见图层</option>
            <option value="all">全部元素</option>
          </select>
        </label>
        <button type="button" data-testid="report-refresh" onClick={() => setReloadNonce((value) => value + 1)} disabled={loading}>
          {loading ? "生成中…" : "刷新"}
        </button>
      </div>

      {report ? <div className="report-counts" data-testid="report-counts">
        <span>设备 <strong>{report.counts.equipment}</strong></span>
        <span>管线 <strong>{report.counts.lines}</strong></span>
        <span>仪表 <strong>{report.counts.instruments}</strong></span>
      </div> : null}

      <div className="report-tabs" role="tablist" aria-label="工程报表">
        {visibleReportTabs.map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            data-testid={`report-tab-${item}`}
            aria-selected={tab === item}
            className={tab === item ? "active" : ""}
            onClick={() => setTab(item)}
          >
            {reportTabLabels[item]} {report ? reportTabCount(report, item) : 0}
          </button>
        ))}
      </div>

      <div className="report-filter-row">
        <input
          data-testid="report-filter"
          type="search"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="筛选位号、介质、设备或管线…"
        />
      </div>

      <button type="button" className="report-download" data-testid="report-download" onClick={() => void downloadCsv()} disabled={!report || downloading}>
        {downloading ? "下载中…" : `下载 ${reportTabLabels[tab]} CSV`}
      </button>

      {error ? <div className="error-box" data-testid="report-error">{error}</div> : null}
      {!error && loading && !report ? <p className="inspector-hint">正在生成确定性报表…</p> : null}
      {!loading && report && !rows.length ? <p className="inspector-hint">当前筛选没有结果。</p> : null}

      <div className="report-rows" data-testid="report-rows">
        {rows.map((row) => (
          <article className="report-row" data-testid={`report-row-${rowKey(row)}`} key={rowKey(row)}>
            <div className="report-row-heading">
              <strong>{rowTitle(row)}</strong>
              <button type="button" onClick={() => locate(row)} disabled={!reportRowElementIds(row).some((id) => document.elements.some((element) => element.id === id))}>定位</button>
            </div>
            <p>{rowSummary(row)}</p>
            <code>{reportRowElementIds(row).join(", ")}</code>
          </article>
        ))}
      </div>

      {report ? <p className="report-revision" data-testid="report-revision">revision {report.revision} · {report.scope === "visible" ? "可见图层" : "全部元素"}</p> : null}
    </div>
  );
}
