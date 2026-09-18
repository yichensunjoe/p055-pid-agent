import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import {
  connectionMatchLabel,
  connectionSummary,
  engineeringGroupOrder,
  engineeringObjectLabel,
  engineeringObjectSummary,
  filterEngineeringObjects,
  filterFindings,
  groupEngineeringObjects,
  identityBasisLabel,
  kindCount,
  projectFreshnessSummary,
  severityCounts,
  stalenessLabel,
  traceSummary,
} from "../engineeringGraph";
import { useWorkspace } from "../store";
import type {
  EngineeringGraph,
  EngineeringObjectKind,
  EngineeringTraceResult,
  ProjectEngineeringGraph,
} from "../types";

type SeverityFilter = "all" | "error" | "warning" | "info";
type TraceDirection = "upstream" | "downstream" | "both";

const traceDirectionLabels: Record<TraceDirection, string> = {
  both: "上下游",
  downstream: "仅下游",
  upstream: "仅上游",
};

const reasonLabels: Record<string, string> = {
  revision_changed: "revision 变化",
  content_hash_changed: "工程内容变化",
  builder_version_changed: "构建器版本变化",
  document_deleted: "图纸已删除",
};

/**
 * Read-only view of the derived engineering semantic graph (Charter M2).
 *
 * The panel never edits anything: it shows engineering objects with their stable
 * identities, process vs signal connectivity, findings and cross-drawing connections
 * so a reviewer can see what the system derived from the drawing instead of trusting
 * the picture.
 */
export function EngineeringGraphPanel() {
  const document = useWorkspace((state) => state.document);
  const setSelection = useWorkspace((state) => state.setSelection);

  const [graph, setGraph] = useState<EngineeringGraph | null>(null);
  const [project, setProject] = useState<ProjectEngineeringGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [severity, setSeverity] = useState<SeverityFilter>("all");
  const [activeKind, setActiveKind] = useState<EngineeringObjectKind | "all">("all");
  const [traceOrigin, setTraceOrigin] = useState("");
  const [traceDirection, setTraceDirection] = useState<TraceDirection>("both");
  const [trace, setTrace] = useState<EngineeringTraceResult | null>(null);
  const [traceError, setTraceError] = useState("");
  const [traceLoading, setTraceLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);

  const documentId = document?.id;
  const documentRevision = document?.revision;

  useEffect(() => {
    if (!documentId) {
      setGraph(null);
      setTrace(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    void api.getEngineeringGraph(documentId)
      .then((next) => {
        if (!cancelled) setGraph(next);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof ApiError ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [documentId, documentRevision, reloadNonce]);

  useEffect(() => {
    let cancelled = false;
    void api.getProjectEngineeringGraph()
      .then((next) => { if (!cancelled) setProject(next); })
      .catch(() => { /* the project index is optional context for this panel */ });
    return () => { cancelled = true; };
  }, [reloadNonce]);

  useEffect(() => {
    setTraceOrigin("");
    setTrace(null);
    setTraceError("");
  }, [documentId]);

  const groups = useMemo(() => groupEngineeringObjects(graph), [graph]);
  const visibleGroups = useMemo(
    () => groups
      .filter((group) => activeKind === "all" || group.kind === activeKind)
      .map((group) => ({ ...group, objects: filterEngineeringObjects(group.objects, filter) }))
      .filter((group) => group.objects.length > 0),
    [groups, activeKind, filter],
  );
  const counts = useMemo(() => severityCounts(graph?.findings ?? []), [graph]);
  const visibleFindings = useMemo(
    () => filterFindings(graph?.findings ?? [], severity),
    [graph, severity],
  );

  const selectObject = (elementIds: string[]) => {
    const available = new Set((document?.elements ?? []).map((element) => element.id));
    const selectable = elementIds.filter((elementId) => available.has(elementId));
    if (selectable.length) setSelection(selectable, { revealProperties: false });
  };

  const runTrace = async (ref: string, direction: TraceDirection) => {
    if (!documentId || !ref) return;
    setTraceLoading(true);
    setTraceError("");
    try {
      const result = await api.getEngineeringTrace(documentId, ref, direction);
      setTrace(result);
      // Map the reached identities back to drawing elements so the canvas highlights
      // what the trace actually walked.
      const elementIds = result.steps.flatMap(
        (step) => graph?.objects.find((object) => object.engineering_id === step.engineering_id)?.element_ids ?? [],
      );
      selectObject(elementIds);
    } catch (reason) {
      setTrace(null);
      setTraceError(reason instanceof ApiError ? reason.message : String(reason));
    } finally {
      setTraceLoading(false);
    }
  };

  const rebuildIndex = async () => {
    setRebuilding(true);
    setError("");
    try {
      await api.rebuildProjectIndex();
      setProject(await api.getProjectEngineeringGraph());
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : String(reason));
    } finally {
      setRebuilding(false);
    }
  };

  if (!document) return <p className="inspector-hint">打开文档后可查看工程语义图。</p>;

  return (
    <div className="engineering-graph-panel" data-testid="engineering-graph-panel">
      <div className="graph-counts" data-testid="graph-counts">
        {engineeringGroupOrder.map((kind) => (
          <button
            key={kind}
            type="button"
            data-testid={`graph-count-${kind}`}
            className={activeKind === kind ? "active" : ""}
            onClick={() => setActiveKind(activeKind === kind ? "all" : kind)}
          >
            {kindCount(graph?.counts ?? emptyCounts, kind)}
          </button>
        ))}
        <span data-testid="graph-edge-count">
          工艺边 {graph?.counts.process_edges ?? 0} · 信号边 {graph?.counts.signal_edges ?? 0}
        </span>
        <span data-testid="graph-signal-count">信号 {graph?.counts.signals ?? 0}</span>
      </div>

      <div className="graph-toolbar">
        <input
          data-testid="graph-filter"
          type="search"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="筛选位号、管线号、介质、对象 ID…"
        />
        <button type="button" data-testid="graph-refresh" onClick={() => setReloadNonce((value) => value + 1)} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      <div className="graph-tabs" role="tablist" aria-label="findings 严重度">
        {(["all", "error", "warning", "info"] as SeverityFilter[]).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            data-testid={`graph-severity-${item}`}
            aria-selected={severity === item}
            className={severity === item ? "active" : ""}
            onClick={() => setSeverity(item)}
          >
            {item === "all" ? `全部 ${graph?.findings.length ?? 0}` : `${item} ${counts[item as "error" | "warning" | "info"]}`}
          </button>
        ))}
      </div>

      {error ? <div className="error-box" data-testid="graph-error">{error}</div> : null}
      {!error && loading && !graph ? <p className="inspector-hint">正在推导工程语义图…</p> : null}

      <div className="graph-rows" data-testid="graph-findings">
        {visibleFindings.map((finding) => (
          <article className={`graph-row severity-${finding.severity}`} data-testid={`graph-finding-${finding.code}`} key={`${finding.code}-${finding.object_ids.join("-")}-${finding.message}`}>
            <div className="graph-row-heading">
              <strong>{finding.code}</strong>
              <button
                type="button"
                disabled={!finding.element_ids.length}
                onClick={() => selectObject(finding.element_ids)}
              >
                定位
              </button>
            </div>
            <p>{finding.message}</p>
            {finding.object_ids.length ? <code>{finding.object_ids.join(", ")}</code> : null}
            {finding.element_ids.length ? <code>{finding.element_ids.join(", ")}</code> : null}
          </article>
        ))}
        {!loading && graph && !visibleFindings.length ? (
          <p className="inspector-hint">当前筛选没有 findings。</p>
        ) : null}
      </div>

      <div className="graph-groups" data-testid="graph-groups">
        {visibleGroups.map((group) => (
          <section key={group.kind} data-testid={`graph-group-${group.kind}`}>
            <h3>{group.label} · {group.objects.length}{group.count !== group.objects.length ? ` / ${group.count}` : ""}</h3>
            {group.objects.map((object) => (
              <article className="graph-object" data-testid={`graph-object-${object.engineering_id}`} key={object.engineering_id}>
                <div className="graph-object-heading">
                  <strong>{engineeringObjectLabel(object)}</strong>
                  <span title={object.declared_id ? `声明标识 ${object.declared_id}` : undefined}>
                    {identityBasisLabel(object)}
                  </span>
                </div>
                <p>{engineeringObjectSummary(object)}</p>
                <code data-testid={`graph-identity-${object.engineering_id}`}>{object.engineering_id}</code>
                {object.tag_key ? <code>{object.tag_key}</code> : null}
                <div className="graph-object-actions">
                  <button type="button" onClick={() => selectObject(object.element_ids)}>定位</button>
                  <button
                    type="button"
                    data-testid={`graph-trace-${object.engineering_id}`}
                    onClick={() => {
                      setTraceOrigin(object.engineering_id);
                      void runTrace(object.engineering_id, traceDirection);
                    }}
                  >
                    追踪
                  </button>
                </div>
              </article>
            ))}
          </section>
        ))}
      </div>

      <div className="graph-trace" data-testid="graph-trace">
        <label>追踪方向
          <select
            data-testid="graph-trace-direction"
            value={traceDirection}
            onChange={(event) => {
              const next = event.target.value as TraceDirection;
              setTraceDirection(next);
              if (traceOrigin) void runTrace(traceOrigin, next);
            }}
          >
            {(Object.keys(traceDirectionLabels) as TraceDirection[]).map((item) => (
              <option key={item} value={item}>{traceDirectionLabels[item]}</option>
            ))}
          </select>
        </label>
        {traceError ? <div className="error-box" data-testid="graph-trace-error">{traceError}</div> : null}
        {traceLoading ? <p className="inspector-hint">正在追踪…</p> : null}
        {trace ? (
          <>
            <p data-testid="graph-trace-summary">{traceSummary(trace)}</p>
            <ol data-testid="graph-trace-steps">
              {trace.steps.map((step) => (
                <li key={`${step.depth}-${step.engineering_id}-${step.via_connector_id}`}>
                  <span>{step.engineering_id}</span>
                  <em>{step.direction === "origin" ? "起点" : step.direction === "downstream" ? "下游" : step.direction === "upstream" ? "上游" : "未声明方向"}</em>
                  {step.via_connector_id ? <code>{step.via_connector_id}</code> : null}
                </li>
              ))}
            </ol>
          </>
        ) : null}
      </div>

      <div className="graph-project" data-testid="graph-project">
        <div className="graph-row-heading">
          <strong>项目工程索引</strong>
          <button type="button" data-testid="graph-rebuild-index" onClick={() => void rebuildIndex()} disabled={rebuilding}>
            {rebuilding ? "重建中…" : "重建索引"}
          </button>
        </div>
        <p data-testid="graph-project-summary">{projectFreshnessSummary(project)}</p>
        {project ? (
          <>
            <ul className="graph-project-totals" data-testid="graph-project-totals">
              <li>对象 {project.totals.objects}</li>
              <li>设备 {project.totals.equipment}</li>
              <li>阀门 {project.totals.valves}</li>
              <li>仪表 {project.totals.instruments}</li>
              <li>信号 {project.totals.signals}</li>
              <li>管线 {project.totals.lines}</li>
              <li>跨图 {project.totals.off_page_connectors}</li>
              <li>跨图连接 {project.off_page_connections.length}</li>
            </ul>
            {project.stale_document_ids.length ? (
              <p className="graph-stale" data-testid="graph-project-stale">
                过期图纸：{project.stale_document_ids.join(", ")}
              </p>
            ) : null}
            <ul className="graph-cross-links" data-testid="graph-cross-links">
              {project.off_page_connections.slice(0, 12).map((connection) => (
                <li
                  key={connection.connection_id}
                  className={connection.resolved ? "resolved" : "unresolved"}
                  data-testid={`graph-connection-${connection.connection_id}`}
                >
                  <span>{connection.source_tag || connection.source_engineering_id}</span>
                  <em>{connection.direction === "out" ? "出口" : connection.direction === "in" ? "入口" : "—"}</em>
                  <code>{connection.target_document_id || "未声明目标"}</code>
                  <strong>{connectionSummary(connection)}</strong>
                  <code>{connectionMatchLabel(connection.matched_by)}</code>
                  <code>{connection.connection_id}</code>
                </li>
              ))}
            </ul>
            {project.documents.length ? (
              <ul className="graph-project-documents" data-testid="graph-project-documents">
                {project.documents.slice(0, 12).map((entry) => (
                  <li key={entry.document_id}>
                    <span>{entry.document_name}</span>
                    <code>r{entry.revision}</code>
                    <em>{stalenessLabel(entry.staleness)}</em>
                    {entry.stale_reasons.length ? (
                      <small>{entry.stale_reasons.map((reason) => reasonLabels[reason] ?? reason).join("、")}</small>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
      </div>

      {graph ? (
        <p className="graph-revision" data-testid="graph-revision">
          revision {graph.revision} · content {graph.content_hash.slice(0, 12)} · 对象 {graph.counts.objects} · 组件 {graph.connectivity_components.length}
        </p>
      ) : null}
    </div>
  );
}

const emptyCounts: EngineeringGraph["counts"] = {
  equipment: 0,
  valves: 0,
  instruments: 0,
  signals: 0,
  lines: 0,
  junctions: 0,
  off_page_connectors: 0,
  annotations: 0,
  graphics: 0,
  objects: 0,
  edges: 0,
  process_edges: 0,
  signal_edges: 0,
  errors: 0,
  warnings: 0,
  info: 0,
};
