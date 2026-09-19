/**
 * Canonical engineering validation, as the UI reads it (M4).
 *
 * The panel is a *viewer*: it shows the canonical result the server produced and never
 * computes a verdict of its own. That is deliberate — a browser that reimplemented
 * severity policy, applied its own waivers or hid issues would be a second validation
 * system, and the one a human is most likely to believe.
 *
 * Everything in this module is pure so it can be unit-tested without a DOM or a server.
 */

export type ValidationSeverity = "info" | "warning" | "error" | "blocker";
export type WaiverStatus = "not_waived" | "waived" | "expired";

export type ValidationIssue = {
  code: string;
  severity: ValidationSeverity;
  object_ids: string[];
  element_ids: string[];
  message: string;
  expected: string;
  actual: string;
  suggested_repair: string;
  rule_source: string;
  registered: boolean;
  threshold: number | null;
  waiver_status: WaiverStatus;
  waiver: {
    waiver_id: string;
    actor: string;
    reason: string;
    granted_at: string;
    expires_at: string | null;
    status: WaiverStatus;
  } | null;
  validator_id: string;
  rule_id: string;
  profile_id: string;
  profile_version: string;
  details: Record<string, unknown>;
};

export type ValidationCounts = {
  blocker: number;
  error: number;
  warning: number;
  info: number;
  total: number;
  waived: number;
  unregistered: number;
};

export type ValidationResult = {
  document_id: string;
  revision: number;
  content_hash: string;
  profile_id: string;
  profile_version: string;
  rule_bundle_fingerprint: string;
  engine_version: string;
  symbol_registry_fingerprint: string;
  evaluated_at: string;
  validators_run: string[];
  validators_skipped: { validator_id: string; code: string; reason: string }[];
  issues: ValidationIssue[];
  counts: ValidationCounts;
  result_hash: string;
};

export type ReleaseReadiness = {
  document_id: string;
  revision: number;
  /** Readiness carries the same provenance bindings as the validation result it names. */
  profile_id: string;
  profile_version: string;
  rule_bundle_fingerprint: string;
  state: "eligible" | "not_eligible";
  reasons: string[];
  missing_required_validators: string[];
  unwaived_blockers: string[];
  unregistered_codes: string[];
  waivers_considered: string[];
  counts: ValidationCounts;
  human_approval_required: true;
  validation_hash: string;
  readiness_hash: string;
  evaluated_at: string;
};

export const severityOrder: ValidationSeverity[] = ["blocker", "error", "warning", "info"];

export const severityLabels: Record<ValidationSeverity, string> = {
  blocker: "阻断",
  error: "错误",
  warning: "警告",
  info: "提示",
};

export const waiverLabels: Record<WaiverStatus, string> = {
  not_waived: "未豁免",
  waived: "已豁免",
  expired: "豁免已过期",
};

/** Rank used for display order; the server's canonical order is authoritative. */
export function severityRank(severity: ValidationSeverity): number {
  return severityOrder.indexOf(severity);
}

/**
 * Sort worst-first for a reviewer, keeping the server's order as the tie-break so the
 * list is stable between reloads of the same result.
 */
export function sortIssues(issues: ValidationIssue[]): ValidationIssue[] {
  return issues
    .map((issue, index) => ({ issue, index }))
    .sort((left, right) => {
      const bySeverity = severityRank(left.issue.severity) - severityRank(right.issue.severity);
      if (bySeverity !== 0) return bySeverity;
      const byCode = left.issue.code.localeCompare(right.issue.code);
      if (byCode !== 0) return byCode;
      return left.index - right.index;
    })
    .map((entry) => entry.issue);
}

export type IssueFilter = ValidationSeverity | "all" | "waived";

export function filterIssues(
  issues: ValidationIssue[],
  filter: string,
  severity: IssueFilter = "all",
): ValidationIssue[] {
  const query = filter.trim().toLocaleLowerCase();
  return issues.filter((issue) => {
    if (severity === "waived" && issue.waiver_status !== "waived") return false;
    if (severity !== "all" && severity !== "waived" && issue.severity !== severity) return false;
    if (!query) return true;
    return searchableText(issue).includes(query);
  });
}

function searchableText(issue: ValidationIssue): string {
  return [
    issue.code,
    issue.severity,
    issue.message,
    issue.expected,
    issue.actual,
    issue.rule_source,
    issue.rule_id,
    ...issue.object_ids,
    ...issue.element_ids,
  ]
    .join(" ")
    .toLocaleLowerCase();
}

/** The fields a reviewer needs to judge one issue, in reading order. */
export function issueDetailRows(issue: ValidationIssue): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = [];
  if (issue.object_ids.length) rows.push({ label: "工程对象", value: issue.object_ids.join(", ") });
  if (issue.element_ids.length) rows.push({ label: "图元", value: issue.element_ids.join(", ") });
  if (issue.expected || issue.actual) {
    rows.push({
      label: "期望 / 实际",
      value: `${issue.expected || "—"} / ${issue.actual || "—"}`,
    });
  }
  if (issue.threshold !== null) {
    rows.push({ label: "生效阈值", value: String(issue.threshold) });
  }
  rows.push({ label: "规则来源", value: issue.registered ? issue.rule_source : `${issue.rule_source}（未注册）` });
  if (issue.suggested_repair) rows.push({ label: "建议修复", value: issue.suggested_repair });
  rows.push({ label: "豁免状态", value: waiverLabels[issue.waiver_status] });
  if (issue.waiver) {
    rows.push({
      label: "豁免证据",
      value: `${issue.waiver.waiver_id} · ${issue.waiver.actor} · ${issue.waiver.reason}`,
    });
  }
  return rows;
}

/**
 * The bindings that make a validation result and a readiness verdict one review state.
 *
 * Readiness re-runs validation server-side, so asking for the two in separate requests
 * with no explicit evaluation time gives them two different `now` values: the readiness
 * payload then references a result hash the reviewer is not looking at, and near a waiver
 * boundary the waiver state itself can differ. The UI therefore sends one explicit
 * evaluation instant to both requests and refuses to present the pair as coherent unless
 * they describe the same revision, profile, rule bundle and moment (M4 R3 P0-5).
 */
export type EvaluationBinding = {
  bound: boolean;
  mismatches: string[];
};

export function bindEvaluation(
  result: ValidationResult,
  readiness: ReleaseReadiness,
): EvaluationBinding {
  const mismatches: string[] = [];
  if (result.document_id !== readiness.document_id) mismatches.push("document_id");
  if (result.revision !== readiness.revision) mismatches.push("revision");
  if (result.profile_id !== readiness.profile_id) mismatches.push("profile_id");
  if (result.profile_version !== readiness.profile_version) mismatches.push("profile_version");
  if (result.rule_bundle_fingerprint !== readiness.rule_bundle_fingerprint) {
    mismatches.push("rule_bundle_fingerprint");
  }
  if (readiness.evaluated_at !== result.evaluated_at) mismatches.push("evaluated_at");
  if (readiness.validation_hash !== result.result_hash) mismatches.push("validation_hash");
  return { bound: mismatches.length === 0, mismatches };
}

/** How a mismatch reads to the reviewer: the pair is not one piece of evidence. */
export function bindingNotice(binding: EvaluationBinding): string {
  if (binding.bound) return "校验与就绪检查绑定同一版本与同一评估时刻。";
  return `校验结果与就绪证据不一致（${binding.mismatches.join("、")}），不能作为同一份评审证据，请重新校验。`;
}

export function hasBlockingWork(result: ValidationResult): boolean {
  return result.issues.some((issue) => issue.severity === "blocker" && issue.waiver_status !== "waived");
}

/** A short, honest statement of what the automated gate does *not* do. */
export function approvalDisclaimer(): string {
  return "校验与就绪检查只产生证据：它们不会批准、签发或放行图纸，人工审批仍是独立门。";
}
