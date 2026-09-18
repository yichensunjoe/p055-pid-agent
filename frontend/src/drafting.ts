import type { Document, Element, Operation } from "./types";
import type {
  DraftingCrossing,
  DraftingFinding,
  DraftingGate,
  DraftingJunction,
  DraftingLocks,
  DraftingMetrics,
  DraftingOptions,
  DraftingPreview,
  DraftingReport,
  DraftingSeverity,
  DraftingSnapshot,
} from "./draftingTypes";

/**
 * Pure presentation/decision helpers for the drafting panel (Charter M3).
 *
 * Everything here is deterministic and side-effect free: it turns the engine's report
 * into what a human must decide. In particular the gate explanation keeps the two rule
 * sets apart — drafting rules (this engine) versus drawing rules (`diagram_quality`) —
 * because "the pipe crosses the legend" and "the label sits on a symbol" are different
 * defects with different fixes.
 */

export const DRAFTING_SEVERITY_ORDER: DraftingSeverity[] = [
  "blocker",
  "error",
  "warning",
  "info",
];

const SEVERITY_LABELS: Record<DraftingSeverity, string> = {
  blocker: "阻塞",
  error: "错误",
  warning: "警告",
  info: "提示",
};

/** Lock flag stored on an element, so a manual pin survives reloads and travels with the file. */
export const DRAFTING_LOCK_KEY = "drafting_lock";

export function severityLabel(severity: DraftingSeverity): string {
  return SEVERITY_LABELS[severity] ?? severity;
}

export function isWaived(finding: DraftingFinding): boolean {
  return finding.waived === true;
}

export function activeFindings(findings: DraftingFinding[]): DraftingFinding[] {
  return findings.filter((finding) => !isWaived(finding));
}

export function countBySeverity(
  findings: DraftingFinding[],
): Record<DraftingSeverity, number> {
  const counts: Record<DraftingSeverity, number> = {
    blocker: 0,
    error: 0,
    warning: 0,
    info: 0,
  };
  for (const finding of findings) {
    if (isWaived(finding)) continue;
    counts[finding.severity] = (counts[finding.severity] ?? 0) + 1;
  }
  return counts;
}

/** Findings grouped by code, worst severity first, so one defect reads as one row. */
export type DraftingFindingGroup = {
  code: string;
  severity: DraftingSeverity;
  ruleSource: string;
  waived: boolean;
  count: number;
  elementIds: string[];
  messages: string[];
};

export function groupFindings(findings: DraftingFinding[]): DraftingFindingGroup[] {
  const groups = new Map<string, DraftingFindingGroup>();
  for (const finding of findings) {
    const existing = groups.get(finding.code);
    if (existing === undefined) {
      groups.set(finding.code, {
        code: finding.code,
        severity: finding.severity,
        ruleSource: finding.rule_source,
        waived: isWaived(finding),
        count: 1,
        elementIds: [...finding.element_ids],
        messages: [finding.message],
      });
      continue;
    }
    existing.count += 1;
    existing.waived = existing.waived && isWaived(finding);
    existing.messages.push(finding.message);
    for (const elementId of finding.element_ids) {
      if (!existing.elementIds.includes(elementId)) existing.elementIds.push(elementId);
    }
    if (severityRank(finding.severity) < severityRank(existing.severity)) {
      existing.severity = finding.severity;
    }
  }
  return [...groups.values()].sort(
    (left, right) =>
      severityRank(left.severity) - severityRank(right.severity) ||
      left.code.localeCompare(right.code),
  );
}

function severityRank(severity: DraftingSeverity): number {
  const index = DRAFTING_SEVERITY_ORDER.indexOf(severity);
  return index === -1 ? DRAFTING_SEVERITY_ORDER.length : index;
}

export function ruleSourceLabel(ruleSource: string): string {
  if (ruleSource === "diagram_quality") return "图面规则";
  if (ruleSource === "drafting") return "整理规则";
  return ruleSource;
}

/**
 * Why the gate is closed, in the order a reviewer must act on it:
 * drafting blockers first (the engine's own structural findings), then drawing-rule
 * errors, then the score target.
 */
export function gateReasons(gate: DraftingGate): string[] {
  const reasons: string[] = [];
  for (const finding of gate.blockers) {
    reasons.push(`[${finding.code}] ${finding.message}`);
  }
  for (const issue of gate.drawing_issues.filter((issue) => issue.severity === "error")) {
    reasons.push(`[${issue.code}] ${issue.message}`);
  }
  if (gate.score < gate.target_score) {
    reasons.push(`图面评分 ${gate.score} 低于目标 ${gate.target_score}`);
  }
  return reasons;
}

export function gateVerdict(gate: DraftingGate): { label: string; tone: "pass" | "fail" } {
  return gate.passed
    ? { label: "通过", tone: "pass" }
    : { label: "未通过", tone: "fail" };
}

export function gateSummary(gate: DraftingGate): string {
  const verdict = gateVerdict(gate);
  return `${verdict.label} · 评分 ${gate.score}/${gate.target_score} · 整理阻塞 ${gate.blockers.length} · 图面错误 ${gate.drawing_issues.filter((issue) => issue.severity === "error").length}`;
}

/** The hard metrics, as a before/after table. Lower is better for every one of them. */
const HARD_METRIC_LABELS: Array<[keyof DraftingSnapshot, string, number]> = [
  ["node_overlaps", "设备/节点重叠", 0],
  ["pipe_obstacle_intersections", "管线穿越设备", 0],
  ["geometric_crossings", "几何交叉", 0],
  ["unbridged_crossings", "未加跨线桥", 0],
  ["non_orthogonal_segments", "斜线段", 0],
  ["micro_segments", "微小线段", 0],
  ["unnecessary_bends", "多余拐点", 0],
  ["total_bends", "拐点总数", 0],
  ["text_text_overlaps", "标注互相重叠", 0],
  ["text_symbol_overlaps", "标注压住设备", 0],
  ["text_connector_intersections", "标注压住管线", 0],
  ["duplicate_label_count", "重复标注", 0],
  ["out_of_bounds_symbols", "越界设备", 0],
  ["out_of_bounds_connector_points", "越界管线点", 0],
  ["reserved_region_intrusions", "侵入预留区域", 0],
  ["dangling_junction_count", "悬空连接节点", 0],
  ["error_issue_count", "错误问题数", 0],
  ["total_route_length", "管线总长", 1],
];

export type DraftingMetricRow = {
  key: string;
  label: string;
  digits: number;
  before: number;
  after: number;
  delta: number;
  improved: boolean;
  worsened: boolean;
};

export function metricRows(metrics: DraftingMetrics): DraftingMetricRow[] {
  return HARD_METRIC_LABELS.map(([key, label, digits]) => {
    const before = Number(metrics.before[key] ?? 0);
    const after = Number(metrics.after[key] ?? 0);
    const delta = round(after - before, digits);
    return {
      key: String(key),
      label,
      digits,
      before: round(before, digits),
      after: round(after, digits),
      delta,
      improved: delta < -1e-9,
      worsened: delta > 1e-9,
    };
  }).filter((row) => row.before !== 0 || row.after !== 0);
}

export function metricValue(value: number, digits = 0): string {
  return digits > 0 ? value.toFixed(digits) : String(Math.round(value));
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

/** What the run actually did, in one line, or that it found nothing to do. */
export function previewSummary(preview: DraftingPreview): string {
  if (preview.settled || preview.transaction === null) {
    return "无需改动：图纸已满足本次整理条件。";
  }
  const parts = [
    `移动 ${preview.moved_element_ids.length}`,
    `重排管线 ${preview.rerouted_connector_ids.length}`,
    `移动标注 ${preview.moved_annotation_ids.length}`,
    `加跨线桥 ${preview.bridged_connector_ids.length}`,
  ];
  return `将修改 ${preview.transaction.operations.length} 处：${parts.join(" · ")}`;
}

export function previewScopeLabel(preview: DraftingPreview): string {
  return `基于 r${preview.current_revision} · 范围内 ${preview.in_scope_element_ids.length} 个元素 · 锁定 ${preview.locked_element_ids.length}`;
}

export function lockSummary(locks: DraftingLocks): string {
  const parts: string[] = [];
  if (locks.metadata_element_ids.length) {
    parts.push(`手动锁定 ${locks.metadata_element_ids.length}`);
  }
  if (locks.request_element_ids.length) {
    parts.push(`本次临时锁定 ${locks.request_element_ids.length}`);
  }
  if (locks.region_element_ids.length) {
    parts.push(`锁定区域 ${locks.region_labels.join("、") || "未命名"}（${locks.region_element_ids.length} 个元素）`);
  }
  return parts.length ? parts.join(" · ") : "没有锁定元素";
}

export function crossingsSummary(crossings: DraftingCrossing[]): string {
  if (!crossings.length) return "没有交叉";
  const bridged = crossings.filter((crossing) => crossing.bridged && !crossing.at_junction_id).length;
  const onJunction = crossings.filter((crossing) => crossing.at_junction_id).length;
  return `交叉 ${crossings.length} · 已加跨线桥 ${bridged} · 落在连接节点上 ${onJunction}`;
}

export function junctionsSummary(junctions: DraftingJunction[]): string {
  if (!junctions.length) return "没有连接节点";
  const branches = junctions.filter((junction) => junction.kind === "branch").length;
  const inline = junctions.filter((junction) => junction.kind === "inline").length;
  const dangling = junctions.filter((junction) => junction.kind === "dangling").length;
  return `连接节点 ${junctions.length} · 分支 ${branches} · 管线分段 ${inline} · 悬空 ${dangling}`;
}

/** Element ids that carry a persistent manual lock. */
export function lockedElementIds(document: Document | null | undefined): string[] {
  if (!document) return [];
  return document.elements
    .filter((element) => isLockedElement(element))
    .map((element) => element.id);
}

export function isLockedElement(element: Element): boolean {
  const value = (element as { metadata?: Record<string, unknown> }).metadata?.[
    DRAFTING_LOCK_KEY
  ];
  if (typeof value === "string") {
    return ["1", "true", "yes", "on", "locked"].includes(value.trim().toLowerCase());
  }
  return value === true;
}

/**
 * The operation that pins or unpins elements. It is an ordinary document transaction, so
 * a lock is a reviewed, audited, undoable edit — never hidden panel state.
 */
export function lockOperations(elementIds: string[], locked: boolean): Operation[] {
  return [...new Set(elementIds)]
    .filter((elementId) => elementId.length > 0)
    .sort()
    .map((elementId) => ({
      op: "update_element" as const,
      element_id: elementId,
      patch: { metadata: { [DRAFTING_LOCK_KEY]: locked } },
    }));
}

export function lockLabel(locked: boolean): string {
  return locked ? "锁定所选" : "解除锁定";
}

export function reportSummary(report: DraftingReport): string {
  return `r${report.revision} · ${scopeKindLabel(report.scope_kind)} · 端口 ${report.ports.length} · findings ${report.findings.length} · 引擎 v${report.engine_version}`;
}

export function scopeKindLabel(scope: DraftingReport["scope_kind"]): string {
  if (scope === "region") return "区域内整理";
  if (scope === "selection") return "按选择整理";
  return "整张图";
}

export function reproducibilitySummary(preview: DraftingPreview): string {
  const { reproducibility } = preview;
  return `引擎 v${reproducibility.engine_version} · 操作 ${reproducibility.operation_count} · 摘要 ${reproducibility.transaction_digest.slice(0, 12)}`;
}

/** Build the request the engine expects from the panel's switches. */
export function buildDraftingOptions(input: {
  revision: number;
  scope: "document" | "selection";
  selectedElementIds: string[];
  direction: "horizontal" | "vertical";
  targetScore: number;
  relayout: boolean;
  rerouteConnectors: boolean;
  placeAnnotations: boolean;
  bridgeCrossings: boolean;
  resolveCollisions: boolean;
  waivedCodes: string[];
}): DraftingOptions {
  return {
    expected_revision: input.revision,
    element_ids: input.scope === "selection" ? input.selectedElementIds : [],
    direction: input.direction,
    relayout: input.relayout,
    reroute_connectors: input.rerouteConnectors,
    place_annotations: input.placeAnnotations,
    bridge_crossings: input.bridgeCrossings,
    resolve_collisions: input.resolveCollisions,
    policy: {
      target_score: input.targetScore,
      waived_codes: input.waivedCodes,
    },
  };
}

/**
 * A preview is only applicable to the revision it was computed from. Anything else is a
 * silent overwrite of someone else's edit, so the panel refuses.
 */
export function previewAppliesTo(preview: DraftingPreview, revision: number): boolean {
  return preview.transaction !== null && preview.current_revision === revision;
}

export function stalePreviewMessage(preview: DraftingPreview, revision: number): string {
  return `整理预览基于 r${preview.current_revision}，当前文档已是 r${revision}。请重新生成预览。`;
}

export function summariseElementIds(elementIds: string[], limit = 6): string {
  if (!elementIds.length) return "—";
  if (elementIds.length <= limit) return elementIds.join(", ");
  return `${elementIds.slice(0, limit).join(", ")} … (+${elementIds.length - limit})`;
}
