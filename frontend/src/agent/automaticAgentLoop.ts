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
 */

import type {
  AgentTransactionAssessment,
  SemanticAgentPlanResult,
} from "../types";

/** Matches the server-side attempt ceiling; the loop reuses it rather than inventing a second one. */
export const MAX_REPLANS = 5;

export type AutomaticAgentCompleteness = "complete" | "partial" | "empty" | "unknown";

export type AutomaticAgentVerdict = {
  attempt: number;
  planId: string;
  accounting: "evaluated" | "not_evaluated";
  valid: boolean;
  completeness: AutomaticAgentCompleteness;
  proposed: number | null;
  accepted: number | null;
  rejected: number | null;
  rejectedReasonCodes: string[];
  issueCodes: string[];
  /** The only condition under which a proposal may reach a human for apply. */
  mayProceed: boolean;
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
  presentation: AutomaticAgentCompletenessPresentation;
  rejected: AutomaticAgentRejectedOperation[];
  /** One line a human can read, e.g. `提案 197 · 接受 120 · 拒绝 77 · 完整性 partial`. */
  summary: string;
  /** Repeat-detection key: the same counts *and* the same reasons mean the same dead end. */
  signature: string;
};

function accountingOf(assessment: AgentTransactionAssessment): "evaluated" | "not_evaluated" {
  return assessment.operation_accounting === "evaluated" ? "evaluated" : "not_evaluated";
}

/**
 * An unevaluated assessment reports `null`, and `unknown` is not `complete`.
 *
 * A response that omits the axis entirely (an older server, a hand-built fixture) is treated the
 * same way: unknown. That direction is deliberate -- the failure being removed here was an
 * optimistic default, so the fallback refuses to proceed rather than assuming wholeness.
 */
function completenessOf(assessment: AgentTransactionAssessment): AutomaticAgentCompleteness {
  if (accountingOf(assessment) !== "evaluated") return "unknown";
  const completeness = assessment.completeness;
  if (completeness === "complete" || completeness === "partial" || completeness === "empty") {
    return completeness;
  }
  return "unknown";
}

export function automaticAgentVerdict(result: SemanticAgentPlanResult): AutomaticAgentVerdict {
  const assessment = result.assessment;
  const accounting = accountingOf(assessment);
  const completeness = completenessOf(assessment);
  const evaluated = accounting === "evaluated";
  return {
    attempt: result.attempt,
    planId: result.plan.plan_id,
    accounting,
    valid: assessment.valid,
    completeness,
    proposed: evaluated ? assessment.semantic_operation_count : null,
    accepted: evaluated ? assessment.accepted_operation_count ?? null : null,
    rejected: evaluated ? assessment.rejected_operation_count ?? null : null,
    rejectedReasonCodes: (assessment.rejected_operations ?? []).map(
      (receipt) => receipt.reason_code,
    ),
    issueCodes: assessment.issues.map((issue) => issue.code),
    mayProceed: evaluated && assessment.valid && completeness === "complete",
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
      : `提案 ${count(verdict.proposed)} · 未逐项评估（${verdict.issueCodes.join(", ") || "无诊断"}）`;
  return {
    attempt: verdict.attempt,
    planId: verdict.planId,
    presentation: completenessPresentation(result),
    rejected,
    summary,
    signature: [
      verdict.completeness,
      verdict.accepted,
      verdict.rejected,
      ...verdict.rejectedReasonCodes,
      ...verdict.issueCodes,
    ].join("|"),
  };
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
 * is repaired rather than retried blindly, and an unevaluated plan is asked again rather than
 * treated as empty. Only a valid, complete, evaluated proposal returns.
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
