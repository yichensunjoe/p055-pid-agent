/**
 * The automatic agent's decision loop, kept out of the React component so it can be tested.
 *
 * The loop used to ask one question -- `assessment.valid` -- and stop the moment the answer was
 * yes. A plan that lost 39% of its own operations answered yes, so the loop stopped, the session
 * was recorded as completed, and nobody was told. The question is now the two-axis one: a
 * proposal may only be offered to a human when the compiler actually accounted for it, it is
 * valid, *and* it is complete. Anything else asks for the next proposal, carrying the receipt of
 * what was rejected into that next attempt.
 *
 * Where the receipt goes is worth being precise about, because there are two copies of it and only
 * one is authoritative. The server recompiles the failed plan it is handed and renders the
 * rejected operations into the replan prompt itself; that is the copy the model reads. The copy
 * assembled here exists so the loop can decide (is this the same failure as last time?) and so the
 * human can see the arithmetic. It is deliberately not re-sent as prompt text, which would say the
 * same thing twice in two different renderings.
 *
 * Six branches, declared in the backend contract as `ASSESSMENT_BRANCHES`, and the last of them is
 * why this module has one more concept than "unknown": an assessment that *says* it was not
 * evaluated (`not_evaluated`, carrying the server's `global_failure_reason`) is a real answer and
 * is recovered from like an invalid plan, while an assessment that does not carry the accounting
 * contract at all is a protocol violation. The two must not share a path: replaying the model
 * against a malformed envelope would spend the run's attempts hiding a server defect.
 */

import type {
  AgentTransactionAssessment,
  SemanticAgentPlanResult,
} from "../types";

/** Matches the server-side attempt ceiling; the loop reuses it rather than inventing a second one. */
export const MAX_REPLANS = 5;

export type AutomaticAgentCompleteness = "complete" | "partial" | "empty" | "unknown";

/**
 * The exhaustive set of routes an assessment may take, named to match the backend contract's
 * `ASSESSMENT_BRANCHES`. Six, not five, because "the response is malformed" is not a business
 * outcome: a legitimate `not_evaluated` answer is a real refusal worth recovering from, while
 * an absent accounting contract is a server defect, and replaying the model against a broken
 * contract would spend the run's attempts hiding it.
 */
export type AutomaticAgentBranch =
  | "evaluated_complete"
  | "evaluated_partial"
  | "evaluated_empty"
  | "evaluated_invalid"
  | "not_evaluated"
  | "assessment_contract_violation";

export type AutomaticAgentVerdict = {
  attempt: number;
  planId: string;
  accounting: "evaluated" | "not_evaluated" | "violation";
  branch: AutomaticAgentBranch;
  /** Missing or contradictory fields, when the branch is the contract violation one. */
  contractViolation: string[];
  /** The server's own reason for an unevaluated proposal, shown rather than inferred. */
  globalFailureReason: string;
  valid: boolean;
  completeness: AutomaticAgentCompleteness;
  proposed: number | null;
  accepted: number | null;
  rejected: number | null;
  rejectedReasonCodes: string[];
  issueCodes: string[];
  /** The only condition under which a proposal may reach a human for apply. */
  mayProceed: boolean;
  /** False only for the contract violation: that branch must not consume replan attempts. */
  mayReplanAutomatically: boolean;
};

/**
 * What the confirmation surface must show. The snake_case names are the contract's declared
 * presentation fields, so the interface and the contract cannot drift apart by renaming a label.
 */
export type AutomaticAgentCompletenessPresentation = {
  proposed_operation_count: number | null;
  accepted_operation_count: number | null;
  rejected_operation_count: number | null;
  completeness: AutomaticAgentCompleteness;
};

export type AutomaticAgentRejectedOperation = {
  originalIndex: number;
  operationKind: string;
  reasonCode: string;
  message: string;
  fieldPath: string;
  suggestions: string[];
};

export type AutomaticAgentReceipt = {
  attempt: number;
  planId: string;
  branch: AutomaticAgentBranch;
  presentation: AutomaticAgentCompletenessPresentation;
  rejected: AutomaticAgentRejectedOperation[];
  /** One line a human can read, e.g. `提案 197 · 接受 120 · 拒绝 77 · 完整性 partial`. */
  summary: string;
  /** Repeat-detection key: the same counts *and* the same reasons mean the same dead end. */
  signature: string;
};

const EVALUATED_REQUIRED_FIELDS = [
  "accepted_operation_count",
  "rejected_operation_count",
  "completeness",
  "rejected_operations",
] as const;

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

/**
 * Why this response does not carry the accounting contract, if it does not.
 *
 * Returned as a list of reasons rather than a boolean so the message a human reads can name
 * the offending fields. The three ways a response can be malformed are deliberately distinct
 * from `not_evaluated`: a missing axis, an `evaluated` envelope without its counts or verdict,
 * and a `not_evaluated` envelope that fabricated counts. None of them is a partial plan.
 */
export function assessmentContractViolation(result: SemanticAgentPlanResult): string[] {
  const assessment = result.assessment;
  const accounting = assessment.operation_accounting;
  if (accounting !== "evaluated" && accounting !== "not_evaluated") {
    return [
      `operation_accounting=${accounting === undefined ? "缺失" : String(accounting)}`,
    ];
  }
  const violations: string[] = [];
  if (accounting === "not_evaluated") {
    // The declared invariant: an unevaluated proposal must say why it stopped, and must not
    // report counts it never computed. Zero is a claim, not a stand-in for unknown.
    if (!assessment.global_failure_reason) {
      violations.push("global_failure_reason 缺失");
    }
    for (const field of ["accepted_operation_count", "rejected_operation_count"] as const) {
      if (assessment[field] !== undefined && assessment[field] !== null) {
        violations.push(`${field} 在 not_evaluated 下不得给出计数`);
      }
    }
    return violations;
  }
  for (const field of EVALUATED_REQUIRED_FIELDS) {
    const value = (assessment as Record<string, unknown>)[field];
    if (field === "completeness") {
      if (value !== "complete" && value !== "partial" && value !== "empty") {
        violations.push(`completeness=${value === undefined ? "缺失" : String(value)}`);
      }
      continue;
    }
    if (field === "rejected_operations") {
      if (!Array.isArray(value)) violations.push("rejected_operations 缺失");
      continue;
    }
    if (!isCount(value)) {
      violations.push(`${field}=${value === undefined ? "缺失" : String(value)}`);
    }
  }
  return violations;
}

/**
 * An unevaluated or malformed assessment reports `null`, and `unknown` is not `complete`.
 *
 * The direction is deliberate: the failure being removed here was an optimistic default, so an
 * assessment that makes no claim refuses to proceed rather than assuming wholeness.
 */
function completenessOf(
  accounting: "evaluated" | "not_evaluated" | "violation",
  assessment: AgentTransactionAssessment,
): AutomaticAgentCompleteness {
  if (accounting !== "evaluated") return "unknown";
  const completeness = assessment.completeness;
  if (completeness === "complete" || completeness === "partial" || completeness === "empty") {
    return completeness;
  }
  return "unknown";
}

export function automaticAgentVerdict(result: SemanticAgentPlanResult): AutomaticAgentVerdict {
  const assessment = result.assessment;
  const contractViolation = assessmentContractViolation(result);
  const accounting: AutomaticAgentVerdict["accounting"] =
    contractViolation.length > 0
      ? "violation"
      : assessment.operation_accounting === "evaluated"
        ? "evaluated"
        : "not_evaluated";
  const completeness = completenessOf(accounting, assessment);
  const evaluated = accounting === "evaluated";
  const branch: AutomaticAgentBranch =
    accounting === "violation"
      ? "assessment_contract_violation"
      : accounting === "not_evaluated"
        ? "not_evaluated"
        : !assessment.valid
          ? "evaluated_invalid"
          : completeness === "complete"
            ? "evaluated_complete"
            : completeness === "partial"
              ? "evaluated_partial"
              : "evaluated_empty";
  return {
    attempt: result.attempt,
    planId: result.plan.plan_id,
    accounting,
    branch,
    contractViolation,
    globalFailureReason: assessment.global_failure_reason ?? "",
    valid: assessment.valid,
    completeness,
    proposed: evaluated ? assessment.semantic_operation_count : null,
    accepted: evaluated ? (assessment.accepted_operation_count as number) : null,
    rejected: evaluated ? (assessment.rejected_operation_count as number) : null,
    rejectedReasonCodes: (assessment.rejected_operations ?? []).map(
      (receipt) => receipt.reason_code,
    ),
    issueCodes: assessment.issues.map((issue) => issue.code),
    mayProceed: branch === "evaluated_complete",
    mayReplanAutomatically: branch !== "assessment_contract_violation",
  };
}

export function completenessPresentation(
  result: SemanticAgentPlanResult,
): AutomaticAgentCompletenessPresentation {
  const verdict = automaticAgentVerdict(result);
  return {
    proposed_operation_count: verdict.proposed,
    accepted_operation_count: verdict.accepted,
    rejected_operation_count: verdict.rejected,
    completeness: verdict.completeness,
  };
}

function count(value: number | null): string {
  return value === null ? "?" : String(value);
}

export function automaticAgentReceipt(result: SemanticAgentPlanResult): AutomaticAgentReceipt {
  const verdict = automaticAgentVerdict(result);
  const rejected = (result.assessment.rejected_operations ?? []).map((receipt) => ({
    originalIndex: receipt.original_index,
    operationKind: receipt.operation_kind,
    reasonCode: receipt.reason_code,
    message: receipt.message,
    fieldPath: receipt.field_path,
    suggestions: receipt.suggestions,
  }));
  const summary =
    verdict.accounting === "evaluated"
      ? `提案 ${count(verdict.proposed)} · 接受 ${count(verdict.accepted)} · ` +
        `拒绝 ${count(verdict.rejected)} · 完整性 ${verdict.completeness}`
      : verdict.accounting === "not_evaluated"
        ? `提案 ${count(verdict.proposed)} · 未逐项评估（${
            verdict.globalFailureReason || verdict.issueCodes.join(", ") || "无诊断"
          }）`
        : `响应契约不兼容：${verdict.contractViolation.join("；")}`;
  return {
    attempt: verdict.attempt,
    planId: verdict.planId,
    presentation: completenessPresentation(result),
    rejected,
    summary,
    signature: [
      verdict.branch,
      verdict.completeness,
      verdict.accepted,
      verdict.rejected,
      ...verdict.contractViolation,
      ...verdict.rejectedReasonCodes,
      ...verdict.issueCodes,
    ].join("|"),
    branch: verdict.branch,
  };
}

/**
 * Raised for a response that does not carry the accounting contract.
 *
 * It is a distinct type because the caller must not treat it as one more repair round: this
type of failure says nothing about the model's proposal, so "asking again" would mask a broken
contract behind a bounded number of model requests.
 */
export class AssessmentContractViolation extends Error {
  readonly violations: string[];

  constructor(violations: string[]) {
    super(`响应契约不兼容：${violations.join("；")}`);
    this.name = "AssessmentContractViolation";
    this.violations = violations;
  }
}

export type AutomaticAgentReplanRequest = {
  failed: SemanticAgentPlanResult;
  nextAttempt: number;
  /** The receipt of the proposal being replaced, for bookkeeping and for the trace. */
  receipt: AutomaticAgentReceipt;
};

export type AutomaticAgentDriveOptions = {
  maxReplans?: number;
  isCancelled?: () => boolean;
  onVerdict?: (verdict: AutomaticAgentVerdict, receipt: AutomaticAgentReceipt) => void;
};

export type AutomaticAgentDriveResult = {
  result: SemanticAgentPlanResult;
  verdicts: AutomaticAgentVerdict[];
  receipts: AutomaticAgentReceipt[];
};

/**
 * Drive proposals until one may proceed, asking for the next one after every other outcome.
 *
 * `invalid` keeps its existing recovery path (that path *is* a replan), a valid but partial plan
 * is repaired rather than retried blindly, and an unevaluated plan is known to be unevaluated
 * rather than mistaken for empty. Only a valid, complete, evaluated proposal returns; a response
 * that violates the assessment contract throws immediately, before an attempt is spent.
 */
export async function driveAutomaticAgentPlan(
  first: SemanticAgentPlanResult,
  replan: (request: AutomaticAgentReplanRequest) => Promise<SemanticAgentPlanResult>,
  options: AutomaticAgentDriveOptions = {},
): Promise<AutomaticAgentDriveResult> {
  const maxReplans = options.maxReplans ?? MAX_REPLANS;
  const seen = new Set<string>();
  const verdicts: AutomaticAgentVerdict[] = [];
  const receipts: AutomaticAgentReceipt[] = [];
  let result = first;

  for (;;) {
    if (options.isCancelled?.()) throw new Error("自动执行已停止");
    const verdict = automaticAgentVerdict(result);
    const receipt = automaticAgentReceipt(result);
    verdicts.push(verdict);
    receipts.push(receipt);
    options.onVerdict?.(verdict, receipt);

    if (verdict.mayProceed) return { result, verdicts, receipts };

    // A malformed response ends the run here, before any attempt is spent. Replanning it would
    // hide a contract defect behind the model's attempts: no proposal can repair a missing
    // accounting axis, only a fresh response or a refresh can.
    if (!verdict.mayReplanAutomatically) {
      throw new AssessmentContractViolation(verdict.contractViolation);
    }

    // The same counts with the same reasons is the same dead end, so stop instead of looping.
    if (receipt.signature && seen.has(receipt.signature)) {
      throw new Error(`检测到重复失败循环：${receipt.summary}`);
    }
    seen.add(receipt.signature);

    if (result.attempt >= maxReplans) {
      throw new Error(`达到最大重规划次数 ${maxReplans}（${receipt.summary}）`);
    }

    result = await replan({
      failed: result,
      nextAttempt: result.attempt + 1,
      receipt,
    });
  }
}
