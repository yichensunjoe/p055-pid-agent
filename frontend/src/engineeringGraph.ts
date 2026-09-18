import type {
  EngineeringGraph,
  EngineeringGraphFinding,
  EngineeringObject,
  EngineeringObjectKind,
  EngineeringTraceResult,
  ProjectEngineeringGraph,
  ProjectIndexEntry,
} from "./types";

/** Object kinds shown as engineering content, in the order an engineer reads them. */
export const engineeringGroupOrder: EngineeringObjectKind[] = [
  "equipment",
  "valve",
  "instrument",
  "line",
  "junction",
  "off_page_connector",
  "annotation",
  "graphic",
];

export const engineeringGroupLabels: Record<EngineeringObjectKind, string> = {
  equipment: "设备",
  valve: "阀门",
  instrument: "仪表",
  line: "管线",
  junction: "连接节点",
  off_page_connector: "跨图连接",
  annotation: "标注",
  graphic: "图元",
};

/** Counts key per kind; the backend count names differ from the object kind names. */
export function kindCount(counts: EngineeringGraph["counts"], kind: EngineeringObjectKind): number {
  switch (kind) {
    case "equipment": return counts.equipment;
    case "valve": return counts.valves;
    case "instrument": return counts.instruments;
    case "line": return counts.lines;
    case "junction": return counts.junctions;
    case "off_page_connector": return counts.off_page_connectors;
    case "annotation": return counts.annotations;
    case "graphic": return counts.graphics;
  }
}

export type EngineeringGroup = {
  kind: EngineeringObjectKind;
  label: string;
  count: number;
  objects: EngineeringObject[];
};

export function groupEngineeringObjects(graph: EngineeringGraph | null): EngineeringGroup[] {
  if (!graph) return [];
  return engineeringGroupOrder
    .map((kind) => ({
      kind,
      label: engineeringGroupLabels[kind],
      count: kindCount(graph.counts, kind),
      objects: graph.objects.filter((object) => object.kind === kind),
    }))
    .filter((group) => group.objects.length > 0);
}

export function engineeringObjectLabel(object: EngineeringObject): string {
  return object.tag || object.label || object.symbol_name || object.object_id;
}

/** Secondary line: what the object is and how it is wired. */
export function engineeringObjectSummary(object: EngineeringObject): string {
  const parts: string[] = [];
  if (object.symbol_name) parts.push(object.symbol_name);
  if (object.kind === "line") {
    if (object.media) parts.push(object.media);
    if (object.nominal_diameter) parts.push(object.nominal_diameter);
    if (object.length) parts.push(`${Math.round(object.length)} mm`);
  } else if (object.kind === "off_page_connector") {
    parts.push(object.opc_direction === "in" ? "入口" : object.opc_direction === "out" ? "出口" : "方向未知");
    if (object.target_document_id) parts.push(`→ ${object.target_document_id}`);
  } else if (object.required_port_count) {
    parts.push(`端口 ${object.connected_port_count}/${object.required_port_count}`);
  }
  if (object.identity_scope === "element") parts.push("无位号");
  return parts.join(" · ");
}

export function filterEngineeringObjects(
  objects: EngineeringObject[],
  filter: string,
): EngineeringObject[] {
  const needle = filter.trim().toLowerCase();
  if (!needle) return objects;
  return objects.filter((object) =>
    [
      object.object_id,
      object.tag,
      object.label,
      object.symbol_key,
      object.symbol_name,
      object.medium_class,
      object.nominal_diameter,
      object.target_document_id,
    ]
      .filter(Boolean)
      .some((value) => value.toLowerCase().includes(needle)),
  );
}

export function severityCounts(findings: EngineeringGraphFinding[]): Record<"error" | "warning" | "info", number> {
  return {
    error: findings.filter((finding) => finding.severity === "error").length,
    warning: findings.filter((finding) => finding.severity === "warning").length,
    info: findings.filter((finding) => finding.severity === "info").length,
  };
}

export function filterFindings(
  findings: EngineeringGraphFinding[],
  severity: "all" | "error" | "warning" | "info",
): EngineeringGraphFinding[] {
  if (severity === "all") return findings;
  return findings.filter((finding) => finding.severity === severity);
}

export function traceSummary(result: EngineeringTraceResult | null): string {
  if (!result) return "";
  const head = `${result.origin_object_id} · ${result.direction === "both" ? "上下游" : result.direction === "downstream" ? "下游" : "上游"}`;
  const pipelines = result.traversed_pipeline_ids.length
    ? ` · 经过管线 ${result.traversed_pipeline_ids.join(", ")}`
    : "";
  const truncated = result.truncated ? " · 已截断" : "";
  return `${head} · ${result.steps.length} 个对象${pipelines}${truncated}`;
}

const stalenessLabels: Record<ProjectIndexEntry["staleness"], string> = {
  verified_fresh: "已验证最新",
  fresh: "revision 一致",
  stale: "已过期",
  missing_document: "图纸已不存在",
  builder_outdated: "构建器版本变化",
};

export function stalenessLabel(staleness: ProjectIndexEntry["staleness"]): string {
  return stalenessLabels[staleness] ?? staleness;
}

/** One line describing project-wide freshness; never claims more than it checked. */
export function projectFreshnessSummary(project: ProjectEngineeringGraph | null): string {
  if (!project) return "";
  const verified = project.freshness === "verified" ? "已验证" : "仅比对 revision";
  const stale = project.stale_document_ids.length;
  return [
    `${project.document_count} 张图纸`,
    `已索引 ${project.indexed_document_count}`,
    stale ? `过期 ${stale}` : "无过期",
    verified,
  ].join(" · ");
}
